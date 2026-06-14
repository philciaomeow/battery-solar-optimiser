"""Tests for the Battery Solar Optimiser."""

from datetime import datetime, timedelta, timezone

from custom_components.battery_solar_optimiser.optimiser import build_plan


def _make_rates(base: datetime, n: int = 48, cheap_slot: int = 2) -> list[tuple[datetime, float]]:
    rates = []
    for i in range(n):
        price = 0.05 if i == cheap_slot else 0.30  # GBP/kWh
        rates.append((base + timedelta(minutes=30 * i), price))
    return rates


def _make_solar(base: datetime, n: int = 48, peak_slot: int = 14) -> list[tuple[datetime, float]]:
    solar = []
    for i in range(n):
        val = 1.0 if i == peak_slot else 0.0
        solar.append((base + timedelta(minutes=30 * i), val))
    return solar


def test_basic_plan():
    now = datetime(2026, 6, 14, 0, 0, tzinfo=timezone.utc)
    rates = _make_rates(now, cheap_slot=4)
    solar = _make_solar(now)
    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=0.5,
        current_soc_kwh=2.5,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
    )
    assert len(plan.slots) == 48
    assert plan.initial_soc_kwh == 2.5
    assert all(s.price > 0 for s in plan.slots)
    assert all(s.start.minute in (0, 30) for s in plan.slots)
    assert all(hasattr(s, "slot_cost_gbp") for s in plan.slots)


def test_cheap_charge_triggered():
    now = datetime(2026, 6, 14, 0, 0, tzinfo=timezone.utc)
    rates = _make_rates(now, cheap_slot=2)
    solar = _make_solar(now)
    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=0.5,
        current_soc_kwh=1.0,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
    )
    cheap_slot = plan.slots[2]
    assert cheap_slot.action == "charge"
    assert cheap_slot.action_kw > 0


def test_respects_min_soc():
    now = datetime(2026, 6, 14, 0, 0, tzinfo=timezone.utc)
    rates = [(now + timedelta(minutes=30 * i), 0.30) for i in range(48)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]
    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=1.0,
        current_soc_kwh=0.2,
        load_w=5000,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
    )
    assert min(plan.projected_soc) >= 0.999


def test_aligns_to_half_hour():
    now = datetime(2026, 6, 14, 9, 17, tzinfo=timezone.utc)
    rates = [(now + timedelta(minutes=30 * i), 0.15) for i in range(48)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]
    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=0.5,
        current_soc_kwh=2.5,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
    )
    assert plan.slots[0].start == datetime(2026, 6, 14, 9, 0, tzinfo=timezone.utc)
    assert plan.slots[1].start == datetime(2026, 6, 14, 9, 30, tzinfo=timezone.utc)


def test_missing_rates_use_pessimistic_fallback():
    now = datetime(2026, 6, 14, 20, 0, tzinfo=timezone.utc)
    rates = [(now + timedelta(minutes=30 * i), 0.10) for i in range(4)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]
    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=0.5,
        current_soc_kwh=1.0,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
        missing_rate_pence=30.0,
    )
    assert plan.slots[4].price == 30.0
    assert any(slot.action == "charge" for slot in plan.slots[:4])


def test_hold_does_not_drain_battery_at_flat_price():
    now = datetime(2026, 6, 14, 0, 0, tzinfo=timezone.utc)
    rates = [(now + timedelta(minutes=30 * i), 0.20) for i in range(48)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]
    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=0.5,
        current_soc_kwh=5.0,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
    )
    assert all(slot.action == "hold" for slot in plan.slots)
    assert min(plan.projected_soc) == 5.0


def test_first_expensive_slot_is_discharged():
    now = datetime(2026, 6, 14, 12, 30, tzinfo=timezone.utc)
    # Mirrors the live case: 16:00 is the first peak slot but just below the
    # old 80th percentile threshold, so it must still be discharged.
    pence = [12.684, 13.755, 13.503, 13.923, 14.543, 14.553, 15.729, 33.484, 33.999, 35.343, 36.309, 36.907, 35.553, 21.903]
    rates = [(now + timedelta(minutes=30 * i), p / 100.0) for i, p in enumerate(pence)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]
    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=0.5,
        current_soc_kwh=5.0,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
        missing_rate_pence=30.0,
    )
    assert plan.slots[7].price == 33.484
    assert plan.slots[7].action == "discharge"


def test_full_battery_pre_peak_slots_stay_in_charge_mode():
    now = datetime(2026, 6, 14, 12, 30, tzinfo=timezone.utc)
    pence = [12.684, 13.755, 13.503, 13.923, 14.543, 14.553, 15.729, 33.484, 33.999]
    rates = [(now + timedelta(minutes=30 * i), p / 100.0) for i, p in enumerate(pence)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]
    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=0.5,
        current_soc_kwh=5.0,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
        missing_rate_pence=30.0,
    )
    assert any(slot.action == "charge" for slot in plan.slots[:7])
    assert all(slot.action in ("charge", "discharge") for slot in plan.slots[:9])


def test_missing_future_rates_use_previous_day_same_slot_before_fixed_fallback():
    now = datetime(2026, 6, 14, 20, 0, tzinfo=timezone.utc)
    current_rates = [(now + timedelta(minutes=30 * i), 0.10) for i in range(2)]
    previous_day = [
        (datetime(2026, 6, 13, 21, 0, tzinfo=timezone.utc), 0.07),
        (datetime(2026, 6, 13, 21, 30, tzinfo=timezone.utc), 0.08),
    ]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]
    plan = build_plan(
        now=now,
        agile_rates=current_rates,
        previous_day_rates=previous_day,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=0.5,
        current_soc_kwh=1.0,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
        missing_rate_pence=30.0,
    )
    assert round(plan.slots[2].price, 3) == 7.0
    assert plan.slots[2].price_source == "previous_day"
    assert plan.slots[4].price == 30.0
    assert plan.slots[4].price_source == "fallback"


def test_historical_lookback_influences_price_thresholds():
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    # Upcoming slots alone are tightly clustered around 20p, but the previous
    # slots were extremely cheap. With lookback enabled, 20p is no longer
    # treated as cheap relative to recent prices.
    upcoming = [(now + timedelta(minutes=30 * i), 0.20) for i in range(48)]
    history = [(now - timedelta(minutes=30 * i), 0.02) for i in range(1, 13)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]
    plan = build_plan(
        now=now,
        agile_rates=upcoming,
        historical_rates=[*history, *upcoming],
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=0.5,
        current_soc_kwh=5.0,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
        missing_rate_pence=30.0,
        lookback_hours=6,
    )
    assert any(slot.action == "discharge" for slot in plan.slots[:4])
