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

    # Price thresholds for charge/discharge decisions
    prices = [s.price for s in slots if s.price > 0]
    if len(prices) >= 3:
        sorted_prices = sorted(prices)
        cheap_threshold = sorted_prices[len(sorted_prices) // 3]  # lower third
        expensive_threshold = sorted_prices[(2 * len(sorted_prices)) // 3]  # upper third
    elif prices:
        mean = sum(prices) / len(prices)
        cheap_threshold = mean * 0.85
        expensive_threshold = mean * 1.15
    else:
        cheap_threshold = 999.0
        expensive_threshold = 0.0

    # Effective round-trip cost of storing/imported energy
    breakeven_sell_price = cheap_threshold / efficiency

    soc = max(current_soc_kwh, min_soc_kwh)
    projected_soc = [soc]
    total_import = 0.0
    total_export = 0.0

    for slot in slots:
        net_load = load_per_slot - slot.solar_kwh
        is_cheap = slot.price <= cheap_threshold and slot.price > 0
        is_expensive = slot.price >= expensive_threshold and slot.price > 0
        action = "hold"
        action_kw = 0.0

        if net_load <= 0:
            # Excess solar. Soak into battery if room, otherwise export.
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
            # Need energy. Prefer solar first, then decide battery vs grid.
            if is_expensive and soc > min_soc_kwh + 0.05:
                # Discharge to cover load (and more if profitable)
                available = min(
                    soc - min_soc_kwh,
                    max_discharge_kw * slot_duration_h,
                )
                # Cover at least net load; optionally export more if very profitable
                discharge = min(available, net_load / efficiency)
                if slot.price > breakeven_sell_price * 1.2:
                    # Export surplus at very high prices
                    discharge = available
                    total_export += (discharge * efficiency - net_load)
                soc -= discharge / efficiency
                action = "discharge"
                action_kw = discharge / slot_duration_h
                remaining = net_load - discharge * efficiency
                if remaining > 0:
                    total_import += remaining
            elif is_cheap and soc < battery_capacity_kwh - 0.05:
                # Charge from grid to cover this and future load
                charge = min(
                    battery_capacity_kwh - soc,
                    max_charge_kw * slot_duration_h * efficiency,
                )
                soc += charge
                action = "charge"
                action_kw = charge / slot_duration_h
                total_import += net_load + charge
            else:
                # Neutral price: cover load from battery if convenient, else grid
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
        # importing to cover net load when not charging is captured via total_import but
        # we only estimate battery-driven import cost here for simplicity

    return Plan(
        slots=slots,
        initial_soc_kwh=current_soc_kwh,
        projected_soc=projected_soc,
        estimated_cost_gbp=estimated_cost,
        total_import_kwh=total_import,
        total_export_kwh=total_export,
    )
