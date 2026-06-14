"""Tests for the Battery Solar Optimiser."""

from datetime import datetime, timedelta

import pytest

from custom_components.battery_solar_optimiser.optimiser import build_plan


def _rates(start: datetime, count: int, base_price: float):
    return [
        (start + timedelta(minutes=30 * i), base_price + (i % 10) * 2)
        for i in range(count)
    ]


def _solar(start: datetime, count: int, peak_hour: int = 12):
    out = []
    for i in range(count):
        hour = (start.hour + i // 2) % 24
        val = max(0, 1.5 - abs(hour - peak_hour) * 0.3)
        out.append((start + timedelta(minutes=30 * i), val))
    return out


def test_basic_plan():
    now = datetime(2026, 6, 14, 0, 0, 0)
    rates = _rates(now, 48, 10.0)
    solar = _solar(now, 48)
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
    assert plan is not None
    assert len(plan.slots) == 48
    assert plan.initial_soc_kwh == 2.5
    assert len(plan.projected_soc) == 49


def test_cheap_charge_triggered():
    now = datetime(2026, 6, 14, 0, 0, 0)
    rates = [(now + timedelta(minutes=30 * i), 30.0) for i in range(48)]
    rates[2] = (rates[2][0], 5.0)  # one cheap slot
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
    )
    assert plan.slots[2].action == "charge"


def test_respects_min_soc():
    now = datetime(2026, 6, 14, 0, 0, 0)
    rates = [(now + timedelta(minutes=30 * i), 30.0) for i in range(48)]
    solar = [(now + timedelta(minutes=30 * i), 0.0) for i in range(48)]
    plan = build_plan(
        now=now,
        agile_rates=rates,
        solar_forecast=solar,
        battery_capacity_kwh=5.0,
        min_soc_kwh=2.0,
        current_soc_kwh=2.1,
        load_w=6000,
        max_charge_kw=3.7,
        max_discharge_kw=3.7,
        efficiency=0.95,
    )
    # High load should pull from grid once min SOC is hit
    assert min(plan.projected_soc) >= 2.0 - 1e-6
