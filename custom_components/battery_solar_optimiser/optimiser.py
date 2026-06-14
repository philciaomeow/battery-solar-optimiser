"""Core optimisation logic for Battery Solar Optimiser."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class Slot:
    """One 30-minute optimisation slot."""

    start: datetime
    end: datetime
    price: float  # p/kWh
    solar_kwh: float  # expected solar generation in this slot
    action: str = "hold"  # 'charge', 'discharge', 'hold'
    action_kw: float = 0.0
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    slot_cost_gbp: float = 0.0
    cumulative_cost_gbp: float = 0.0


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
    missing_rate_pence: float = 30.0,
) -> Plan:
    """Build a charge/discharge plan over the next horizon_slots half-hours.

    Prices from Octopus are provided as GBP/kWh and converted to p/kWh.
    Missing future rates are treated pessimistically (default 30p/kWh), not as
    free energy. This avoids draining the battery before unpublished Agile slots.
    """
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
                price=price_map.get(_to_utc(t), missing_rate_pence),
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
        cheap_threshold = missing_rate_pence * 0.75
        expensive_threshold = missing_rate_pence

    # Require a meaningful spread before arbitrage; with mostly flat prices,
    # preserve battery rather than cycling pointlessly.
    price_spread = max(prices) - min(prices) if prices else 0.0
    arbitrage_enabled = price_spread >= 3.0

    soc = max(current_soc_kwh, min_soc_kwh)
    projected_soc = [soc]
    total_import = 0.0
    total_export = 0.0
    cumulative_cost = 0.0

    # First pass: decide what we *want* to do. Normal/cheap slots import house
    # load from grid so battery is kept for genuinely expensive slots. Solar is
    # still soaked into the battery whenever available.
    actions: list[str] = []
    forced: list[bool] = []
    for i, slot in enumerate(slots):
        net_load = load_per_slot - slot.solar_kwh
        future_prices = [slots[j].price for j in range(i + 1, min(i + 16, len(slots)))]
        max_future_price = max(future_prices) if future_prices else slot.price
        avg_future_price = sum(future_prices) / len(future_prices) if future_prices else slot.price

        action = "hold"
        is_forced = False

        if net_load <= 0 and soc < battery_capacity_kwh - 0.05:
            # Excess solar: charge even if energy price is uninteresting.
            action = "charge"
        elif arbitrage_enabled and slot.price <= cheap_threshold and slot.price > 0:
            # Charge before later expensive slots if the spread beats round-trip loss.
            profitable_future = max_future_price > (slot.price / max(efficiency, 0.01)) + 2.0
            if profitable_future and soc < battery_capacity_kwh - 0.05:
                action = "charge"
                is_forced = True
        elif arbitrage_enabled and slot.price >= expensive_threshold and soc > min_soc_kwh + 0.05:
            # Discharge during expensive slots. Do not require a cheaper future slot;
            # the point is to avoid buying now when the battery already has energy.
            if slot.price > avg_future_price or slot.price >= missing_rate_pence:
                action = "discharge"
                is_forced = True

        actions.append(action)
        forced.append(is_forced)

    # Smoothing pass: remove single-slot flips only when not a price/solar-driven action.
    smoothed = actions[:]
    for i in range(1, len(smoothed) - 1):
        if forced[i]:
            continue
        prev_a = smoothed[i - 1]
        curr_a = smoothed[i]
        next_a = smoothed[i + 1]
        if curr_a != prev_a and curr_a != next_a:
            smoothed[i] = prev_a

    # Second pass: simulate with actions, enforcing SOC and calculating per-slot costs.
    for i, slot in enumerate(slots):
        net_load = load_per_slot - slot.solar_kwh
        action = smoothed[i]
        action_kw = 0.0
        slot_import = 0.0
        slot_export = 0.0

        if action == "charge":
            # Charge battery and cover any household deficit from grid.
            available = battery_capacity_kwh - soc
            charge = min(
                available,
                max_charge_kw * slot_duration_h * efficiency,
            )
            if charge > 0.001:
                soc += charge
                action_kw = charge / slot_duration_h
                slot_import += charge / max(efficiency, 0.01)
            else:
                action = "hold"
            slot_import += max(0, net_load)
            slot_export += max(0, -net_load)

        elif action == "discharge":
            available = min(
                max(0, soc - min_soc_kwh),
                max_discharge_kw * slot_duration_h,
            )
            discharge = min(available, max(0, net_load) / max(efficiency, 0.01))
            if discharge > 0.001:
                soc -= discharge / max(efficiency, 0.01)
                action_kw = discharge / slot_duration_h
                remaining = net_load - discharge * efficiency
                slot_import += max(0, remaining)
            else:
                action = "hold"
                slot_import += max(0, net_load)
            slot_export += max(0, -net_load)

        else:
            # Hold: solar can charge the battery, but positive household load is
            # imported from the grid instead of draining the battery at normal prices.
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
                slot_export += max(0, -net_load - charge)
            else:
                slot_import += net_load

        slot.action = action
        slot.action_kw = round(action_kw, 3)
        soc = max(min(soc, battery_capacity_kwh), min_soc_kwh)

        slot.import_kwh = round(slot_import, 3)
        slot.export_kwh = round(slot_export, 3)
        slot.slot_cost_gbp = round(slot_import * (slot.price / 100.0), 4)
        cumulative_cost += slot.slot_cost_gbp
        slot.cumulative_cost_gbp = round(cumulative_cost, 4)

        total_import += slot_import
        total_export += slot_export
        projected_soc.append(soc)

    return Plan(
        slots=slots,
        initial_soc_kwh=current_soc_kwh,
        projected_soc=projected_soc,
        estimated_cost_gbp=round(cumulative_cost, 4),
        total_import_kwh=total_import,
        total_export_kwh=total_export,
    )
