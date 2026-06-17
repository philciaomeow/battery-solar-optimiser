"""Core optimisation logic for Battery Solar Optimiser."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass
class Slot:
    """One 30-minute optimisation slot."""

    start: datetime
    end: datetime
    price: float  # p/kWh
    solar_kwh: float  # expected solar generation in this slot
    price_source: str = "actual"  # actual, previous_day, fallback
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
    previous_day_rates: list[tuple[datetime, float]] | None = None,
    historical_rates: list[tuple[datetime, float]] | None = None,
    lookback_hours: int = 12,
    slot_overrides: dict[int, str] | None = None,
    discharge_aggressiveness: float = 50.0,
    min_arbitrage_spread_pence: float = 3.0,
    display_timezone: str = "Europe/London",
) -> Plan:
    """Build a charge/discharge plan over the next horizon_slots half-hours.

    Prices from Octopus are provided as GBP/kWh and converted to p/kWh.
    Missing future rates are first estimated from the previous day's same
    half-hour slot, then from a fixed pessimistic fallback. Historical rates from
    the recent lookback window are included when deciding what counts as cheap or
    expensive so upcoming prices are not judged in isolation.
    """
    slot_duration_h = 0.5
    load_kw = load_w / 1000.0
    load_per_slot = load_kw * slot_duration_h

    first_slot = _align_to_half_hour(now)
    start_times = [first_slot + timedelta(minutes=30 * i) for i in range(horizon_slots)]

    price_map: dict[datetime, float] = {}
    for t, p in agile_rates:
        price_map[_to_utc(t)] = p * 100.0  # p/kWh

    previous_day_time_map: dict[tuple[int, int], float] = {}
    for t, p in previous_day_rates or []:
        ts = _to_utc(t)
        previous_day_time_map[(ts.hour, ts.minute)] = p * 100.0

    historical_price_map: dict[datetime, float] = {}
    for t, p in historical_rates or agile_rates:
        historical_price_map[_to_utc(t)] = p * 100.0

    solar_map: dict[datetime, float] = {}
    for t, s in solar_forecast:
        solar_map[_to_utc(t)] = s

    slots: list[Slot] = []
    for t in start_times:
        utc_t = _to_utc(t)
        if utc_t in price_map:
            price = price_map[utc_t]
            price_source = "actual"
        else:
            previous_day_price = previous_day_time_map.get((utc_t.hour, utc_t.minute))
            if previous_day_price is not None:
                price = previous_day_price
                price_source = "previous_day"
            else:
                price = missing_rate_pence
                price_source = "fallback"
        slots.append(
            Slot(
                start=t,
                end=t + timedelta(minutes=30),
                price=price,
                solar_kwh=solar_map.get(utc_t, 0.0),
                price_source=price_source,
            )
        )

    lookback_start = first_slot - timedelta(hours=max(0, lookback_hours))
    lookback_prices = [
        price
        for ts, price in historical_price_map.items()
        if lookback_start <= ts < first_slot
    ]
    planning_prices = [s.price for s in slots]
    prices = lookback_prices + planning_prices
    if len(prices) >= 2:
        cheap_threshold = _percentile(prices, 0.20)
        percentile_expensive_threshold = _percentile(prices, 0.80)
    elif prices:
        cheap_threshold = prices[0]
        percentile_expensive_threshold = prices[0]
    else:
        cheap_threshold = missing_rate_pence * 0.75
        percentile_expensive_threshold = missing_rate_pence

    # Require a meaningful spread before arbitrage; with mostly flat prices,
    # preserve battery rather than cycling pointlessly.
    price_spread = max(prices) - min(prices) if prices else 0.0
    arbitrage_enabled = price_spread >= 3.0
    # The 80th percentile can miss the first slot of a peak block by a few
    # tenths of a penny. Use a softer dynamic threshold so the whole expensive
    # block is captured, not just its absolute top.
    expensive_threshold = min(
        percentile_expensive_threshold,
        cheap_threshold + (price_spread * 0.55),
    )
    aggressiveness = max(0.0, min(100.0, float(discharge_aggressiveness)))
    min_arbitrage_spread = max(0.0, float(min_arbitrage_spread_pence))
    # 50 is the neutral default. Higher values lower the expensive threshold so
    # the optimiser spends more of the usable battery during moderately-high
    # Agile periods; lower values preserve more battery for only the worst slots.
    expensive_threshold -= ((aggressiveness - 50.0) / 50.0) * min(price_spread * 0.20, 6.0)

    # Keep the real current SOC. If it is below the configured reserve, the
    # simulation below will actively charge back to the reserve. Clamping here
    # makes the dashboard pretend the battery is already at the reserve and
    # prevents the live action from switching into charge mode.
    soc = max(0.0, min(current_soc_kwh, battery_capacity_kwh))
    projected_soc = [soc]
    total_import = 0.0
    total_export = 0.0

    # First pass: decide what we *want* to do. Normal/cheap slots import house
    # load from grid so battery is kept for genuinely expensive slots. Solar is
    # still soaked into the battery whenever available.
    actions: list[str] = []
    forced: list[bool] = []
    for i, slot in enumerate(slots):
        net_load = load_per_slot - slot.solar_kwh
        future_prices = [slots[j].price for j in range(i + 1, min(i + 16, len(slots)))]
        recent_prices = [slots[j].price for j in range(max(0, i - 16), i)]
        max_future_price = max(future_prices) if future_prices else slot.price
        min_future_price = min(future_prices) if future_prices else slot.price
        avg_future_price = sum(future_prices) / len(future_prices) if future_prices else slot.price
        max_recent_price = max(recent_prices) if recent_prices else slot.price

        action = "hold"
        is_forced = False

        future_expensive = max_future_price >= expensive_threshold
        current_is_discounted = slot.price < (max_future_price - 2.0)

        if net_load <= 0 and soc < battery_capacity_kwh - 0.05:
            # Excess solar: charge even if energy price is uninteresting.
            action = "charge"
        elif arbitrage_enabled and slot.price <= cheap_threshold and slot.price < expensive_threshold:
            # Recharge in genuinely cheap/negative slots, even if they happen
            # after the peak discharge period rather than before it.
            action = "charge"
            is_forced = True
        elif (
            arbitrage_enabled
            and min_arbitrage_spread > 0
            and slot.price <= max_recent_price - min_arbitrage_spread
            and slot.price <= min_future_price + 0.5
            and slot.price < expensive_threshold
        ):
            # Mid-price arbitrage recovery: if we recently avoided buying at a
            # materially higher price, refill on this cheaper slot even if it is
            # not in the absolute cheapest percentile. The simulation turns this
            # back into hold if the battery is already full.
            action = "charge"
            is_forced = True
        else:
            recent_local_prices = [slots[j].price for j in range(max(0, i - 2), i + 1)]
            near_future_prices = [slots[j].price for j in range(i + 1, min(i + 4, len(slots)))]
            local_recharge_before_higher_slot = (
                arbitrage_enabled
                and min_arbitrage_spread > 0
                and near_future_prices
                and slot.price <= min(recent_local_prices) + 0.05
                and max(near_future_prices) >= slot.price + min_arbitrage_spread
            )
            if local_recharge_before_higher_slot:
                # Rolling local arbitrage: when the current slot is a local dip
                # and a higher slot is imminent, charge first so the later slot
                # has usable battery to discharge. This catches 17.5p -> 18.7p
                # and 17.9p -> 19.6p style opportunities controlled by spread.
                action = "charge"
                is_forced = True
            elif (
                arbitrage_enabled
                and min_arbitrage_spread > 0
                and min_future_price <= slot.price - min_arbitrage_spread
                and slot.price >= max(0.0, min_arbitrage_spread)
            ):
                # Mid-price arbitrage: discharge at a moderately expensive price if
                # a cheaper recharge slot is visible soon. This catches cases like
                # 19p now vs 15p later without needing to classify 19p as a peak.
                action = "discharge"
                is_forced = True
            elif arbitrage_enabled and future_expensive and current_is_discounted and slot.price < expensive_threshold:
                # Charge/prepare before later expensive slots if the spread beats
                # round-trip loss, but do not start charging at a merely okay price
                # when clearly cheaper slots are still available before the next
                # expensive block.
                profitable_future = max_future_price > (slot.price / max(efficiency, 0.01)) + 2.0
                near_best_upcoming_price = slot.price <= min_future_price + 0.5
                if profitable_future and near_best_upcoming_price:
                    action = "charge"
                    is_forced = True
            elif arbitrage_enabled and slot.price >= expensive_threshold:
                # Discharge during expensive slots. Do not require the first-pass SOC
                # estimate to be above reserve: earlier planned charge/solar may fill
                # the battery before this slot. The simulation pass enforces the real
                # reserve and reports hold if there is not actually usable energy.
                if (
                    slot.price > avg_future_price
                    or slot.price >= missing_rate_pence
                    or slot.price >= cheap_threshold + 3.0
                ):
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

    def _soc_after_slot(soc_value: float, slot: Slot, action: str) -> float:
        """Approximate SOC movement for pre-discharge charge scheduling.

        Unlike the original helper, this subtracts household load during hold/
        discharge slots so the optimiser does not over-estimate how much energy
        remains at the peak window from early charges.
        """
        net_load = load_per_slot - slot.solar_kwh
        if soc_value < min_soc_kwh - 0.05:
            target_soc = min_soc_kwh
            available = max(0.0, target_soc - soc_value)
            charge = min(available, max_charge_kw * slot_duration_h * efficiency)
            soc_value += charge
        if action == "charge":
            available = max(0.0, battery_capacity_kwh - soc_value)
            charge = min(available, max_charge_kw * slot_duration_h * efficiency)
            soc_value += charge
        # Soak excess solar into the battery regardless of planned action.
        if net_load <= 0:
            available = max(0.0, battery_capacity_kwh - soc_value)
            charge = min(available, -net_load, max_charge_kw * slot_duration_h * efficiency)
            if charge > 0.001:
                soc_value += charge
        else:
            # Household load drains the battery during hold/discharge slots.
            drain = net_load if action != "discharge" else 0.0
            soc_value = max(min_soc_kwh, soc_value - drain)
        return max(0.0, min(battery_capacity_kwh, soc_value))

    def _optimise_pre_discharge_charging(planned: list[str]) -> list[str]:
        """Move discretionary pre-peak charging into the cheapest slots.

        The first pass marks cheap slots greedily. When charge rate is high enough,
        that can start charging earlier than needed and skip later/cheaper slots
        because the battery is already full. For the first upcoming discharge
        window, rebuild the discretionary charge set by selecting the cheapest
        slots before the deadline, breaking near-ties toward later slots.

        We only consider slots within a few hours of the deadline so energy is not
        wasted to household load long before the peak, and the SOC helper above
        accounts for that load.
        """
        try:
            deadline = next(
                idx
                for idx, action in enumerate(planned)
                if action == "discharge" and slots[idx].price >= expensive_threshold
            )
        except StopIteration:
            return planned
        if deadline <= 0:
            return planned

        optimised = planned[:]
        # Only pre-charge in the window immediately before the expensive block.
        # Charging much earlier wastes energy to household load.
        pre_charge_horizon = 12  # 6 hours
        candidates: list[int] = []
        for idx in range(max(0, deadline - pre_charge_horizon), deadline):
            net_load = load_per_slot - slots[idx].solar_kwh
            recent_local_prices = [slots[j].price for j in range(max(0, idx - 2), idx + 1)]
            near_future_prices = [slots[j].price for j in range(idx + 1, min(idx + 4, len(slots)))]
            preserve_local_arbitrage_charge = (
                planned[idx] == "charge"
                and min_arbitrage_spread > 0
                and near_future_prices
                and slots[idx].price <= min(recent_local_prices) + 0.05
                and max(near_future_prices) >= slots[idx].price + min_arbitrage_spread
            )
            if optimised[idx] == "charge" and net_load > 0 and not preserve_local_arbitrage_charge:
                optimised[idx] = "hold"
            if net_load > 0 and slots[idx].price < expensive_threshold:
                candidates.append(idx)

        soc_without_discretionary = soc
        for idx in range(deadline):
            soc_without_discretionary = _soc_after_slot(soc_without_discretionary, slots[idx], optimised[idx])

        needed = max(0.0, battery_capacity_kwh - soc_without_discretionary)
        if needed <= 0.05:
            return optimised

        selected: set[int] = set()
        for idx in sorted(candidates, key=lambda i: (slots[i].price, -i)):
            selected.add(idx)
            needed -= max_charge_kw * slot_duration_h * efficiency
            if needed <= 0.05:
                break
        for idx in selected:
            optimised[idx] = "charge"
        return optimised

    smoothed = _optimise_pre_discharge_charging(smoothed)

    # Manual overrides win over the automatic recommendation and are applied
    # before simulation so projected SOC, costs, and the live action select all
    # reflect what will be sent to the inverter automation.
    for idx, action in (slot_overrides or {}).items():
        if 0 <= idx < len(smoothed) and action in ("charge", "discharge"):
            smoothed[idx] = action

    # Second pass: simulate with actions, enforcing SOC and calculating per-slot costs.
    # Cumulative cost should follow the local day (midnight to midnight), so it
    # resets whenever the slot's local date changes. This keeps the projected
    # daily cost meaningful even though the plan is a rolling window.
    try:
        local_tz = ZoneInfo(display_timezone)
    except ZoneInfoNotFoundError:  # pragma: no cover - tzdata should exist in HA
        local_tz = timezone.utc
    cumulative_cost = 0.0
    last_local_date = None
    for i, slot in enumerate(slots):
        net_load = load_per_slot - slot.solar_kwh
        action = smoothed[i]
        action_kw = 0.0
        slot_import = 0.0
        slot_export = 0.0

        reserve_recovery = soc < min_soc_kwh - 0.05
        if reserve_recovery:
            action = "charge"

        if action == "charge":
            # Charge battery and cover any household deficit from grid.
            target_soc = min_soc_kwh if reserve_recovery else battery_capacity_kwh
            available = max(0.0, target_soc - soc)
            charge = min(
                available,
                max_charge_kw * slot_duration_h * efficiency,
            )
            if charge > 0.001:
                soc += charge
                action_kw = charge / slot_duration_h
                slot_import += charge / max(efficiency, 0.01)
            # If the battery is already full, report hold rather than a 0 kW
            # charge. Otherwise the dashboard appears to tell the inverter to
            # charge at higher/normal prices when there is no useful capacity.
            if charge <= 0.001 and net_load > 0:
                action = "hold"
            slot_import += max(0, net_load)
            slot_export += max(0, -net_load)

        elif action == "discharge":
            available = min(
                max(0, soc - min_soc_kwh),
                max_discharge_kw * slot_duration_h,
            )
            household_discharge_need = max(0, net_load) / max(efficiency, 0.01)
            discharge = min(available, household_discharge_need)
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
        soc = max(0.0, min(soc, battery_capacity_kwh))

        slot.import_kwh = round(slot_import, 3)
        slot.export_kwh = round(slot_export, 3)
        slot.slot_cost_gbp = round(slot_import * (slot.price / 100.0), 4)
        slot_local_date = slot.start.astimezone(local_tz).date()
        if last_local_date is not None and slot_local_date != last_local_date:
            cumulative_cost = 0.0
        last_local_date = slot_local_date
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
