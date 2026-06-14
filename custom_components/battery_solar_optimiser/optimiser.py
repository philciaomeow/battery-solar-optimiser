"""Core optimisation logic for Battery Solar Optimiser."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import logging

_LOGGER = logging.getLogger(__name__)


@dataclass
class Slot:
    """One 30-minute optimisation slot."""

    start: datetime
    end: datetime
    price: float  # p/kWh
    solar_kwh: float  # expected solar generation in this slot
    action: str = "hold"  # 'charge', 'discharge', 'hold'
    action_kw: float = 0.0


@dataclass
class Plan:
    """Result of optimisation."""

    slots: list[Slot]
    initial_soc_kwh: float
    projected_soc: list[float]
    estimated_cost_gbp: float
    total_import_kwh: float
    total_export_kwh: float


def _align_to_half_hour(now: datetime) -> datetime:
    """Round `now` down to the current 30-minute boundary."""
    if now.minute < 30:
        return now.replace(minute=0, second=0, microsecond=0)
    return now.replace(minute=30, second=0, microsecond=0)


def _to_utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _percentile(values: list[float], p: float) -> float:
    """Return the p-th percentile using unique sorted values."""
    if not values:
        return 0.0
    s = sorted(set(values))
    idx = int(round((len(s) - 1) * p))
    return s[idx]


def build_plan(
    *,
    now: datetime,
    agile_rates: list[tuple[datetime, float]],
    solar_forecast: list[tuple[datetime, float]],
    battery_capacity_kwh: float,
    min_soc_kwh: float,
    current_soc_kwh: float,
    load_w: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    efficiency: float,
    horizon_slots: int = 48,
) -> Plan:
    """Build a charge/discharge plan over the next horizon_slots half-hours."""
    slot_duration_h = 0.5
    load_kw = load_w / 1000.0
    load_per_slot = load_kw * slot_duration_h

    first_slot = _align_to_half_hour(now)
    start_times = [first_slot + timedelta(minutes=30 * i) for i in range(horizon_slots)]

    price_map: dict[datetime, float] = {}
    for t, p in agile_rates:
        price_map[_to_utc(t)] = p * 100.0  # p/kWh

    solar_map: dict[datetime, float] = {}
    for t, s in solar_forecast:
        solar_map[_to_utc(t)] = s

    slots: list[Slot] = []
    for t in start_times:
        slots.append(
            Slot(
                start=t,
                end=t + timedelta(minutes=30),
                price=price_map.get(_to_utc(t), 0.0),
                solar_kwh=solar_map.get(_to_utc(t), 0.0),
            )
        )

    prices = [s.price for s in slots if s.price > 0]
    if len(prices) >= 2:
        cheap_threshold = _percentile(prices, 0.20)
        expensive_threshold = _percentile(prices, 0.80)
    elif prices:
        cheap_threshold = prices[0]
        expensive_threshold = prices[0]
    else:
        cheap_threshold = 999.0
        expensive_threshold = 0.0

    # Charge from grid only if there is a future slot expensive enough to justify round-trip loss
    breakeven_discharge_price = cheap_threshold / efficiency if cheap_threshold > 0 else 999.0

    soc = max(current_soc_kwh, min_soc_kwh)
    projected_soc = [soc]
    total_import = 0.0
    total_export = 0.0

    # First pass: determine ideal action based on price and available energy
    actions = []
    forced = []
    for i, slot in enumerate(slots):
        net_load = load_per_slot - slot.solar_kwh
        future_prices = [slots[j].price for j in range(i + 1, min(i + 8, len(slots)))]
        avg_future_price = sum(future_prices) / len(future_prices) if future_prices else slot.price

        action = "hold"
        is_forced = False
        if slot.price <= cheap_threshold and slot.price > 0 and avg_future_price > breakeven_discharge_price:
            if soc < battery_capacity_kwh - 0.05:
                action = "charge"
                is_forced = True
        elif slot.price >= expensive_threshold and slot.price > 0 and soc > min_soc_kwh + 0.05:
            if any(p < slot.price for p in future_prices):
                action = "discharge"
                is_forced = True
        elif net_load <= 0:
            # Excess solar: soak into battery if there is room
            if soc < battery_capacity_kwh - 0.05:
                action = "charge"
        actions.append(action)
        forced.append(is_forced)

    # Smoothing pass: remove single-slot flips only when the slot is NOT a price extreme
    smoothed = actions[:]
    for i in range(1, len(smoothed) - 1):
        if forced[i]:
            continue
        prev_a = smoothed[i - 1]
        curr_a = smoothed[i]
        next_a = smoothed[i + 1]
        if curr_a != prev_a and curr_a != next_a:
            smoothed[i] = prev_a

    # Second pass: simulate with actions, enforcing SOC limits
    for i, slot in enumerate(slots):
        net_load = load_per_slot - slot.solar_kwh
        action = smoothed[i]
        action_kw = 0.0

        if action == "charge":
            # Need to cover net load too if charging from grid
            available = battery_capacity_kwh - soc
            charge = min(
                available,
                max_charge_kw * slot_duration_h * efficiency,
            )
            if charge > 0.001:
                soc += charge
                action_kw = charge / slot_duration_h
                total_import += net_load + charge if net_load > 0 else charge
            else:
                action = "hold"
                total_import += max(0, net_load)
        elif action == "discharge":
            available = min(
                max(0, soc - min_soc_kwh),
                max_discharge_kw * slot_duration_h,
            )
            # Cover at least net load; export any surplus if very profitable
            if slot.price >= breakeven_discharge_price * 1.3:
                discharge = available
            else:
                discharge = min(available, net_load / efficiency)

            if discharge > 0.001:
                soc -= discharge / efficiency
                action_kw = discharge / slot_duration_h
                remaining = net_load - discharge * efficiency
                total_import += max(0, remaining)
                if discharge * efficiency > net_load:
                    total_export += discharge * efficiency - net_load
            else:
                action = "hold"
                total_import += max(0, net_load)
        else:
            # Hold: cover load from solar then battery then grid
            if net_load <= 0:
                available = battery_capacity_kwh - soc
                charge = min(
                    available,
                    max_charge_kw * slot_duration_h * efficiency,
                    -net_load,
                )
                if charge > 0.001:
                    soc += charge
                    action = "charge"
                    action_kw = charge / slot_duration_h
                total_export += max(0, -net_load - charge)
            else:
                available = min(
                    max(0, soc - min_soc_kwh),
                    max_discharge_kw * slot_duration_h,
                )
                if available >= net_load / efficiency:
                    discharge = net_load / efficiency
                    soc -= discharge / efficiency
                    action = "discharge"
                    action_kw = discharge / slot_duration_h
                else:
                    soc -= available / efficiency
                    remaining = net_load - available * efficiency
                    total_import += max(0, remaining)
                    if available > 0.001:
                        action = "discharge"
                        action_kw = available / slot_duration_h

        slot.action = action
        slot.action_kw = round(action_kw, 3)
        soc = max(min(soc, battery_capacity_kwh), min_soc_kwh)
        projected_soc.append(soc)

    estimated_cost = 0.0
    for slot in slots:
        if slot.action == "charge":
            estimated_cost += (slot.action_kw * slot_duration_h) * (slot.price / 100)

    return Plan(
        slots=slots,
        initial_soc_kwh=current_soc_kwh,
        projected_soc=projected_soc,
        estimated_cost_gbp=estimated_cost,
        total_import_kwh=total_import,
        total_export_kwh=total_export,
    )
