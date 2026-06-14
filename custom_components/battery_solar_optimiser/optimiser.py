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
    """Round `now` up to the next 30-minute boundary."""
    if now.minute < 30:
        target = now.replace(minute=30, second=0, microsecond=0)
    else:
        target = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return target


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

    # Align to the next half-hour boundary so slots match Agile slot boundaries
    first_slot = _align_to_half_hour(now)
    start_times = [first_slot + timedelta(minutes=30 * i) for i in range(horizon_slots)]

    # Convert Agile rates from GBP/kWh to p/kWh and build lookup
    price_map: dict[datetime, float] = {}
    for t, p in agile_rates:
        # Normalise to naive UTC if needed for matching
        key = t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
        price_map[key] = p * 100.0  # pence per kWh

    solar_map: dict[datetime, float] = {}
    for t, s in solar_forecast:
        key = t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
        solar_map[key] = s

    slots: list[Slot] = []
    for t in start_times:
        key = t.astimezone(timezone.utc)
        slots.append(
            Slot(
                start=t,
                end=t + timedelta(minutes=30),
                price=price_map.get(key, 0.0),
                solar_kwh=solar_map.get(key, 0.0),
            )
        )

    soc = max(current_soc_kwh, min_soc_kwh)
    projected_soc = [soc]
    total_import = 0.0
    total_export = 0.0

    for slot in slots:
        net_load = load_per_slot - slot.solar_kwh
        if net_load <= 0:
            available = battery_capacity_kwh - soc
            charge = min(
                available,
                max_charge_kw * slot_duration_h * efficiency,
                -net_load,
            )
            soc += charge
            slot.action = "charge"
            slot.action_kw = charge / slot_duration_h
            total_export += max(0, -net_load - charge)
        else:
            available_discharge = min(
                max(0, soc - min_soc_kwh),
                max_discharge_kw * slot_duration_h,
            )
            if available_discharge >= net_load:
                discharge_energy = min(net_load * efficiency, available_discharge)
                soc -= discharge_energy / efficiency
                slot.action = "discharge"
                slot.action_kw = discharge_energy / slot_duration_h
            else:
                soc -= available_discharge / efficiency
                remaining = net_load - available_discharge
                total_import += remaining
                if available_discharge > 0:
                    slot.action = "discharge"
                    slot.action_kw = available_discharge / slot_duration_h
                else:
                    slot.action = "hold"
                    slot.action_kw = 0.0
            soc = max(soc, min_soc_kwh)

        projected_soc.append(soc)

    # Price arbitrage: charge from grid in cheap slots, but never below min_soc after.
    if slots:
        avg_price = sum(s.price for s in slots) / len(slots)
        sorted_by_price = sorted(range(len(slots)), key=lambda i: slots[i].price)

        for idx in sorted_by_price:
            slot = slots[idx]
            if slot.price >= avg_price:
                continue
            future_prices = [
                slots[j].price
                for j in range(idx + 1, min(idx + 12, len(slots)))
            ]
            if not future_prices or slot.price >= max(future_prices, default=0):
                continue
            available = battery_capacity_kwh - projected_soc[idx]
            charge_amount = min(
                available,
                max_charge_kw * slot_duration_h * efficiency,
            )
            if charge_amount <= 0:
                continue
            safe = True
            for k in range(idx, len(slots)):
                if projected_soc[k + 1] + charge_amount < min_soc_kwh:
                    safe = False
                    break
            if not safe:
                continue
            for k in range(idx, len(slots)):
                projected_soc[k + 1] += charge_amount
            slot.action = "charge"
            slot.action_kw += charge_amount / slot_duration_h
            total_import += charge_amount

    estimated_cost = 0.0
    for slot in slots:
        if slot.action == "charge" and slot.price:
            estimated_cost += (slot.action_kw * slot_duration_h) * (slot.price / 100)

    return Plan(
        slots=slots,
        initial_soc_kwh=current_soc_kwh,
        projected_soc=projected_soc,
        estimated_cost_gbp=estimated_cost,
        total_import_kwh=total_import,
        total_export_kwh=total_export,
    )
