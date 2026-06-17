"""Tests for the Battery Solar Optimiser."""

from datetime import datetime, timedelta, timezone

import custom_components.battery_solar_optimiser.sensor as sensor_module
from custom_components.battery_solar_optimiser.optimiser import build_plan
from custom_components.battery_solar_optimiser.sensor import BatterySolarOptimiserCoordinator


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
    assert plan.projected_soc[0] == 0.2
    assert min(plan.projected_soc[1:]) >= 0.999


def test_below_min_soc_charges_to_reserve_instead_of_clamping():
    now = datetime(2026, 6, 14, 0, 0, tzinfo=timezone.utc)
    rates = [(now + timedelta(minutes=30 * i), 0.30) for i in range(48)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]

    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=2.0,
        current_soc_kwh=0.5,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
    )

    assert plan.projected_soc[0] == 0.5
    assert plan.slots[0].action == "charge"
    assert plan.slots[0].action_kw > 0
    assert plan.projected_soc[1] >= 2.0


def test_low_start_can_still_discharge_after_planned_charge():
    now = datetime(2026, 6, 14, 1, 30, tzinfo=timezone.utc)
    pence = [
        19, 18, 17, 17, 18, 18, 19, 18,
        16, 15, 14, 15, 13, 12, 12, 13,
        30, 31, 35, 36, 25, 24, 22, 20,
    ]
    rates = [(now + timedelta(minutes=30 * i), p / 100.0) for i, p in enumerate(pence)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]

    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=1.2,
        current_soc_kwh=1.05,
        load_w=600,
        max_charge_kw=1.0,
        max_discharge_kw=3.7,
        efficiency=0.95,
        discharge_aggressiveness=100,
    )

    assert any(slot.action == "charge" for slot in plan.slots[:16])
    assert any(slot.action == "discharge" for slot in plan.slots[16:20])


def test_precharge_uses_cheapest_slots_before_discharge_deadline():
    now = datetime(2026, 6, 14, 1, 30, tzinfo=timezone.utc)
    # Live-shaped prices: a cheap-ish midday ramp, then the expensive 16:00 peak.
    # At 1.5kW the battery can reach full without using the earlier 14-15p slots,
    # so the optimiser should choose the cheapest slots nearest the deadline.
    pence = [
        19.614, 19.351, 18.459, 17.944, 17.934, 17.913, 17.462, 18.575,
        18.711, 17.861, 19.614, 18.354, 19.005, 16.884, 16.569, 16.790,
        16.863, 15.393, 15.005, 15.393, 14.416, 15.162, 14.595, 13.713,
        12.358, 11.665, 11.560, 13.387, 13.986, 30.145, 30.093, 29.673,
        31.070, 34.828, 35.879, 24.612,
    ]
    rates = [(now + timedelta(minutes=30 * i), p / 100.0) for i, p in enumerate(pence)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]

    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=1.2,
        current_soc_kwh=1.2,
        load_w=600,
        max_charge_kw=1.5,
        max_discharge_kw=3.7,
        efficiency=0.95,
        discharge_aggressiveness=100,
    )

    pre_peak_charge_slots = [idx for idx in range(18, 29) if plan.slots[idx].action == "charge"]

    assert 26 in pre_peak_charge_slots  # 11.56p slot should be used before dearer 14p+ slots.
    assert 20 not in pre_peak_charge_slots  # 14.416p is avoidable at this charge rate.
    assert any(slot.action == "discharge" for slot in plan.slots[29:35])


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


def test_full_battery_pre_peak_slots_report_hold_until_discharge():
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
    assert all(slot.action == "hold" for slot in plan.slots[:7])
    assert plan.slots[7].action == "discharge"


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


def test_negative_slots_after_peak_recharge_battery():
    now = datetime(2026, 6, 14, 16, 0, tzinfo=timezone.utc)
    pence = [36, 35, 34, 23, 22, 21, 8, 4, 1, -2, -4, -6, -8, -7]
    rates = [(now + timedelta(minutes=30 * i), p / 100.0) for i, p in enumerate(pence)]
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
    negative_slots = [slot for slot in plan.slots if slot.price < 0]
    assert negative_slots
    assert any(slot.action == "charge" for slot in negative_slots)
    assert all(slot.action in ("charge", "hold") for slot in negative_slots)
    post_peak_soc = min(plan.projected_soc[1:9])
    later_soc = max(plan.projected_soc[9:15])
    assert later_soc > post_peak_soc


def test_slot_override_forces_action_before_simulation():
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    rates = [(now + timedelta(minutes=30 * i), 0.20) for i in range(48)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]
    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=0.5,
        current_soc_kwh=2.0,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
        slot_overrides={0: "charge", 1: "discharge"},
    )
    assert plan.slots[0].action == "charge"
    assert plan.slots[1].action == "discharge"
    assert plan.projected_soc[1] > plan.projected_soc[0]


def test_discharge_aggressiveness_increases_discharge_slots():
    now = datetime(2026, 6, 14, 14, 0, tzinfo=timezone.utc)
    pence = [18, 20, 23, 25, 27, 29, 31, 34, 36, 35, 30, 28]
    rates = [(now + timedelta(minutes=30 * i), p / 100.0) for i, p in enumerate(pence)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]

    conservative = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=1.0,
        current_soc_kwh=5.0,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
        discharge_aggressiveness=20,
    )
    aggressive = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=1.0,
        current_soc_kwh=5.0,
        load_w=600,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
        discharge_aggressiveness=90,
    )

    conservative_count = sum(slot.action == "discharge" for slot in conservative.slots)
    aggressive_count = sum(slot.action == "discharge" for slot in aggressive.slots)
    assert aggressive_count >= conservative_count


def test_debounced_refresh_cancels_previous_unsubscribe_handle(monkeypatch):
    cancel_calls = []

    def fake_async_call_later(_hass, _delay, _callback):
        def cancel():
            cancel_calls.append("cancelled")

        return cancel

    monkeypatch.setattr(sensor_module, "async_call_later", fake_async_call_later)
    coordinator = BatterySolarOptimiserCoordinator.__new__(BatterySolarOptimiserCoordinator)
    coordinator.hass = object()
    coordinator._debounce_refresh_handle = None

    coordinator.async_request_refresh()
    coordinator.async_request_refresh()

    assert cancel_calls == ["cancelled"]


def test_precharge_waits_for_cheaper_upcoming_slots():
    now = datetime(2026, 6, 15, 23, 30, tzinfo=timezone.utc)
    pence = [19.2, 19.3, 16.4, 16.6, 17.1, 23.0, 24.0, 25.0]
    rates = [(now + timedelta(minutes=30 * i), p / 100.0) for i, p in enumerate(pence)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]

    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=1.0,
        current_soc_kwh=2.0,
        load_w=500,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
        discharge_aggressiveness=100,
    )

    assert plan.slots[0].action == "hold"
    assert plan.slots[1].action == "hold"
    assert plan.slots[2].action == "charge"


def test_full_battery_reports_hold_not_zero_kw_charge():
    now = datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc)
    rates = [(now + timedelta(minutes=30 * i), 0.10 if i < 4 else 0.30) for i in range(48)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]

    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=1.0,
        current_soc_kwh=5.0,
        load_w=500,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
    )

    assert plan.slots[0].action == "hold"
    assert plan.slots[0].action_kw == 0.0
