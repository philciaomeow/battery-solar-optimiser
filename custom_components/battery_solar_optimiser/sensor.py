"""Sensor platform for Battery Solar Optimiser."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_CHARGING,
    ACTION_DISCHARGING,
    ACTION_HOLD,
    DOMAIN,
)
from .optimiser import Plan, build_plan

_LOGGER = logging.getLogger(__name__)


def _map_action(action: str) -> str:
    return {
        "charge": ACTION_CHARGING,
        "discharge": ACTION_DISCHARGING,
        "hold": ACTION_HOLD,
    }.get(action, ACTION_HOLD)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _local_time(value: datetime, time_zone: str = "Europe/London") -> str:
    """Format a datetime in the configured display timezone."""
    try:
        tz = ZoneInfo(time_zone or "Europe/London")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Europe/London")
    return value.astimezone(tz).strftime("%H:%M")


def _current_slot(plan: Plan, now: datetime):
    """Return the slot containing now, or None."""
    for slot in plan.slots:
        if slot.start <= now < slot.end:
            return slot
    return None


def _next_non_hold_slot(plan: Plan, now: datetime):
    """Return the next non-hold slot starting at/after now, or None."""
    for slot in plan.slots:
        if slot.start >= now and _map_action(slot.action) != ACTION_HOLD:
            return slot
    return None


def _contiguous_action_end(plan: Plan, current) -> datetime:
    """Return the end time of the current contiguous action block."""
    end = current.end
    found = False
    for slot in plan.slots:
        if slot is current:
            found = True
            continue
        if not found:
            continue
        if _map_action(slot.action) != _map_action(current.action):
            break
        end = slot.end
    return end


def _solar_heuristic(now: datetime, capacity_kwh: float) -> list[tuple[datetime, float]]:
    """Fallback solar estimate: generate a simple bell curve for today."""
    sunrise = 6
    sunset = 20
    peak = 13
    slots = []
    base = now.replace(minute=0, second=0, microsecond=0)
    for i in range(48):
        slot_start = base + timedelta(minutes=30 * i)
        hour = slot_start.hour + slot_start.minute / 60
        if sunrise <= hour <= sunset:
            factor = max(0, 1 - abs(hour - peak) / ((sunset - sunrise) / 2))
            # Roughly 80% of daily capacity spread across daylight slots
            val = (capacity_kwh * 0.8 / ((sunset - sunrise) * 2)) * factor
        else:
            val = 0.0
        slots.append((slot_start, val))
    return slots


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up the sensor platform."""
    coordinator: BatterySolarOptimiserCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities = [
        BatterySolarOptimiserPlanSensor(coordinator),
        BatterySolarOptimiserCostSensor(coordinator),
        BatterySolarOptimiserNextActionSensor(coordinator),
        BatterySolarOptimiserLastUpdatedSensor(coordinator),
        BatterySolarOptimiserNextUpdateSensor(coordinator),
        BatterySolarOptimiserStatusSensor(coordinator),
    ]
    coordinator.entities.extend(entities)

    async_add_entities(entities)

    # Refresh when the configured source entities become available or change.
    cfg = {**config_entry.data, **config_entry.options}
    source_entities = [
        cfg.get("agile_entity"),
        cfg.get("solar_forecast_entity"),
        cfg.get("battery_soc_entity"),
    ]
    source_entities = [e for e in source_entities if e]

    async def _source_changed(event):
        new_state = event.data.get("new_state")
        if new_state and new_state.state not in (None, "unavailable", "unknown"):
            await coordinator.async_refresh()

    async_track_state_change_event(hass, source_entities, _source_changed)

    # First refresh after 30 seconds, then on the schedule below.
    async_call_later(hass, 30, coordinator.async_refresh)


class BatterySolarOptimiserCoordinator:
    """Holds config and shared state."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        self.hass = hass
        self.config_entry = config_entry
        self.data: dict[str, Any] = {
            "status": "idle",
            "last_updated": None,
            "next_update_due": None,
        }
        self.plan: Plan | None = None
        self.entities: list[SensorEntity] = []
        self._listeners = []

    def _write_entity_states(self) -> None:
        """Push coordinator state to all registered entities."""
        for entity in self.entities:
            if entity.hass:
                entity.async_write_ha_state()

    def _next_scheduled_refresh(self, now: datetime, low_soc: bool = False) -> datetime:
        """Return the next expected refresh time.

        Normal refreshes run at :25 and :55. While battery SOC is below the
        configured minimum, the low-SOC guard checks every five minutes.
        """
        candidates = []
        for hour_offset in range(3):
            base = now + timedelta(hours=hour_offset)
            for minute in (25, 55):
                candidate = base.replace(minute=minute, second=0, microsecond=0)
                if candidate > now:
                    candidates.append(candidate)
        next_due = min(candidates) if candidates else now + timedelta(minutes=30)
        if low_soc:
            next_due = min(next_due, now + timedelta(minutes=5))
        return next_due

    @property
    def cfg(self) -> dict[str, Any]:
        """Return setup data merged with UI options."""
        return {**self.config_entry.data, **self.config_entry.options}

    @property
    def display_timezone(self) -> str:
        """Return the timezone used for human-facing plan times."""
        return str(self.cfg.get("display_timezone", "Europe/London"))

    async def async_refresh(self, now: datetime | None = None) -> None:
        """Refresh optimisation plan and push updates to entities."""
        cfg = self.cfg
        refresh_started = dt_util.utcnow()
        self.data["status"] = "running"
        self._write_entity_states()

        state_api = self.hass.states

        soc_entity = cfg.get("battery_soc_entity", "")
        soc_state = state_api.get(soc_entity)
        try:
            if soc_state is None or soc_state.state in (None, "unavailable", "unknown"):
                current_soc_kwh = cfg.get("min_soc_kwh", 0.0)
            else:
                raw_soc = float(soc_state.state)
                uom = soc_state.attributes.get("unit_of_measurement", "").lower()
                if uom == "%":
                    # Convert percentage to kWh using configured battery capacity
                    current_soc_kwh = raw_soc / 100.0 * cfg.get("battery_capacity_kwh", 5.0)
                else:
                    current_soc_kwh = raw_soc
        except (ValueError, TypeError):
            current_soc_kwh = cfg.get("min_soc_kwh", 0.0)

        agile_entity = cfg.get("agile_entity", "")
        agile_rates: list[tuple[datetime, float]] = []
        rates_state = state_api.get(agile_entity)
        if rates_state and isinstance(rates_state.attributes.get("rates"), list):
            for r in rates_state.attributes["rates"]:
                try:
                    start = _parse_dt(r.get("start"))
                    if start is None:
                        continue
                    price = float(r["value_inc_vat"])
                    agile_rates.append((start, price))
                except (KeyError, ValueError, TypeError):
                    continue

        solar_entity = cfg.get("solar_forecast_entity", "")
        solar_forecast: list[tuple[datetime, float]] = []
        solar_state = state_api.get(solar_entity)
        if solar_state and isinstance(solar_state.attributes.get("forecast"), list):
            for f in solar_state.attributes["forecast"]:
                try:
                    start = _parse_dt(f.get("period_start"))
                    if start is None:
                        continue
                    val = float(f.get("pv_estimate", 0)) / 2  # kWh per 30 min
                    solar_forecast.append((start, val))
                except (KeyError, ValueError, TypeError):
                    continue

        # If no detailed forecast, use a simple heuristic based on installed capacity.
        if not solar_forecast:
            solar_forecast = _solar_heuristic(
                refresh_started, float(cfg.get("pv_capacity_kwh", 5.0))
            )

        low_soc = current_soc_kwh < float(cfg.get("min_soc_kwh", 0.5))
        self.plan = build_plan(
            now=refresh_started,
            agile_rates=agile_rates,
            solar_forecast=solar_forecast,
            battery_capacity_kwh=float(cfg.get("battery_capacity_kwh", 5.0)),
            min_soc_kwh=float(cfg.get("min_soc_kwh", 0.5)),
            current_soc_kwh=current_soc_kwh,
            load_w=float(cfg.get("hourly_load_w", 600)),
            max_charge_kw=float(cfg.get("max_charge_kw", 3.7)),
            max_discharge_kw=float(cfg.get("max_discharge_kw", 3.7)),
            efficiency=float(cfg.get("round_trip_efficiency", 0.95)),
            missing_rate_pence=float(cfg.get("missing_rate_pence", 30.0)),
        )

        refresh_finished = dt_util.utcnow()
        self.data["last_updated"] = refresh_finished
        self.data["next_update_due"] = self._next_scheduled_refresh(refresh_finished, low_soc)
        self.data["status"] = "idle"
        self._write_entity_states()

        # Schedule regular and low-SOC refreshes once, when first entity is added.
        if not self._listeners:
            min_soc_kwh = float(cfg.get("min_soc_kwh", 0.5))
            self._listeners.append(
                async_track_time_change(
                    self.hass,
                    self.async_refresh,
                    minute=[25, 55],
                    second=0,
                )
            )

            async def _low_soc_refresh(_now: datetime) -> None:
                soc_state = self.hass.states.get(soc_entity)
                try:
                    current_soc = float(soc_state.state)
                except (ValueError, TypeError):
                    return
                if current_soc < min_soc_kwh:
                    await self.async_refresh()

            self._listeners.append(
                async_track_time_interval(
                    self.hass,
                    _low_soc_refresh,
                    timedelta(minutes=5),
                )
            )


class BatterySolarOptimiserBaseSensor(SensorEntity, RestoreEntity):
    """Base sensor."""

    _attr_should_poll = False

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=coordinator.config_entry.title,
            manufacturer="Battery Solar Optimiser",
            model="Battery Optimiser",
        )


class BatterySolarOptimiserPlanSensor(BatterySolarOptimiserBaseSensor):
    """Sensor exposing the current charge/discharge plan."""

    _attr_name = "Plan"
    _attr_icon = "mdi:chart-timeline"

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_plan"

    @property
    def native_value(self) -> str:
        plan = self.coordinator.plan
        if not plan or not plan.slots:
            return "unknown"
        now = dt_util.utcnow()
        slot = _current_slot(plan, now)
        if slot:
            return _map_action(slot.action)
        return "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.coordinator.plan
        if not plan:
            return {}
        now = dt_util.utcnow()
        current = _current_slot(plan, now)
        tz = self.coordinator.display_timezone
        capacity = float(self.coordinator.cfg.get("battery_capacity_kwh", 5.0)) or 1.0
        slots = [
            {
                "start": s.start.isoformat(),
                "start_local": _local_time(s.start, tz),
                "price": round(s.price, 3),
                "solar_kwh": round(s.solar_kwh, 3),
                "action": _map_action(s.action),
                "battery_percent": round((plan.projected_soc[idx + 1] / capacity) * 100, 1),
                "slot_cost_gbp": s.slot_cost_gbp,
                "cumulative_cost_gbp": s.cumulative_cost_gbp,
                "is_current": s is current,
            }
            for idx, s in enumerate(plan.slots)
        ]
        return {
            "slots": slots,
            "current_slot_start": current.start.isoformat() if current else None,
            "current_slot_end": current.end.isoformat() if current else None,
            "current_slot_start_local": _local_time(current.start, tz) if current else None,
            "current_slot_end_local": _local_time(current.end, tz) if current else None,
            "initial_soc_kwh": plan.initial_soc_kwh,
            "total_import_kwh": round(plan.total_import_kwh, 3),
            "total_export_kwh": round(plan.total_export_kwh, 3),
        }


class BatterySolarOptimiserCostSensor(BatterySolarOptimiserBaseSensor):
    """Sensor showing estimated cost over the planning horizon."""

    _attr_name = "Estimated Cost"
    _attr_icon = "mdi:currency-gbp"
    _attr_native_unit_of_measurement = "GBP"

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_estimated_cost"

    @property
    def native_value(self) -> float | None:
        plan = self.coordinator.plan
        if not plan:
            return None
        return round(plan.estimated_cost_gbp, 4)


class BatterySolarOptimiserNextActionSensor(BatterySolarOptimiserBaseSensor):
    """Sensor showing the next upcoming action."""

    _attr_name = "Next Action"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_next_action"

    @property
    def native_value(self) -> str:
        plan = self.coordinator.plan
        if not plan or not plan.slots:
            return "unknown"
        now = dt_util.utcnow()
        current = _current_slot(plan, now)
        tz = self.coordinator.display_timezone
        if current and _map_action(current.action) != ACTION_HOLD:
            end = _contiguous_action_end(plan, current)
            return f"{_map_action(current.action)} until {_local_time(end, tz)}"

        next_slot = _next_non_hold_slot(plan, now)
        if next_slot:
            return f"{_map_action(next_slot.action)} at {_local_time(next_slot.start, tz)}"
        return ACTION_HOLD

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.coordinator.plan
        if not plan or not plan.slots:
            return {}
        now = dt_util.utcnow()
        current = _current_slot(plan, now)
        next_slot = _next_non_hold_slot(plan, now)
        tz = self.coordinator.display_timezone
        return {
            "current_slot_action": _map_action(current.action) if current else ACTION_HOLD,
            "current_slot_power_kw": round(current.action_kw, 3) if current else 0,
            "current_slot_start_local": _local_time(current.start, tz) if current else None,
            "current_slot_end_local": _local_time(current.end, tz) if current else None,
            "next_non_hold_action": _map_action(next_slot.action) if next_slot else ACTION_HOLD,
            "next_non_hold_start_local": _local_time(next_slot.start, tz) if next_slot else None,
        }


class BatterySolarOptimiserLastUpdatedSensor(BatterySolarOptimiserBaseSensor):
    """Sensor showing when the plan was last recalculated."""

    _attr_name = "Last Updated"
    _attr_icon = "mdi:clock-check-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_last_updated"

    @property
    def native_value(self) -> datetime | None:
        value = self.coordinator.data.get("last_updated")
        return value if isinstance(value, datetime) else None


class BatterySolarOptimiserNextUpdateSensor(BatterySolarOptimiserBaseSensor):
    """Sensor showing when the next scheduled refresh is expected."""

    _attr_name = "Next Update Due"
    _attr_icon = "mdi:clock-start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_next_update_due"

    @property
    def native_value(self) -> datetime | None:
        value = self.coordinator.data.get("next_update_due")
        return value if isinstance(value, datetime) else None


class BatterySolarOptimiserStatusSensor(BatterySolarOptimiserBaseSensor):
    """Sensor showing whether the optimiser is idle or recalculating."""

    _attr_name = "Status"
    _attr_icon = "mdi:progress-clock"

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_status"

    @property
    def native_value(self) -> str:
        return str(self.coordinator.data.get("status") or "idle")
