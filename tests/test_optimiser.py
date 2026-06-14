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
