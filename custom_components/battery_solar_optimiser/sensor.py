"""Sensor platform for Battery Solar Optimiser."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.dt import utcnow

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
    ]
    coordinator.entities.extend(entities)

    async_add_entities(entities)

    # Refresh when the source entities become available or change.
    source_entities = [
        config_entry.data.get("agile_entity"),
        config_entry.data.get("solar_forecast_entity"),
        config_entry.data.get("battery_soc_entity"),
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
        self.data: dict[str, Any] = {}
        self.plan: Plan | None = None
        self.entities: list[SensorEntity] = []
        self._listeners = []

    async def async_refresh(self, now: datetime | None = None) -> None:
        """Refresh optimisation plan and push updates to entities."""
        cfg = self.config_entry.data
        state_api = self.hass.states

        soc_state = state_api.get(cfg.get("battery_soc_entity", ""))
        try:
            current_soc_kwh = float(soc_state.state) if soc_state else 0.0
        except (ValueError, TypeError):
            current_soc_kwh = 0.0

        agile_entity = cfg.get("agile_entity", "")
        agile_rates: list[tuple[datetime, float]] = []
        rates_state = state_api.get(agile_entity)
        _LOGGER.info(
            "BSO source entities: agile=%s present=%s, solar=%s present=%s, soc=%s present=%s",
            agile_entity,
            state_api.get(agile_entity) is not None,
            solar_entity,
            state_api.get(solar_entity) is not None,
            cfg.get("battery_soc_entity"),
            state_api.get(cfg.get("battery_soc_entity", "")) is not None,
        )
        if rates_state and isinstance(rates_state.attributes.get("rates"), list):
            for r in rates_state.attributes["rates"]:
                try:
                    start = datetime.fromisoformat(r["start"])
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
                    start = datetime.fromisoformat(f["period_start"])
                    val = float(f.get("pv_estimate", 0)) / 2  # kWh per 30 min
                    solar_forecast.append((start, val))
                except (KeyError, ValueError, TypeError):
                    continue

        _LOGGER.info(
            "BSO refresh: rates=%d solar=%d soc=%.2f",
            len(agile_rates), len(solar_forecast), current_soc_kwh)
        self.plan = build_plan(
            now=utcnow(),
            agile_rates=agile_rates,
            solar_forecast=solar_forecast,
            battery_capacity_kwh=float(cfg.get("battery_capacity_kwh", 5.0)),
            min_soc_kwh=float(cfg.get("min_soc_kwh", 0.5)),
            current_soc_kwh=current_soc_kwh,
            load_w=float(cfg.get("hourly_load_w", 600)),
            max_charge_kw=float(cfg.get("max_charge_kw", 3.7)),
            max_discharge_kw=float(cfg.get("max_discharge_kw", 3.7)),
            efficiency=float(cfg.get("round_trip_efficiency", 0.95)),
        )

        self.data["last_updated"] = utcnow().isoformat()

        for entity in self.entities:
            if entity.hass:
                entity.async_write_ha_state()

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
                soc_state = self.hass.states.get(cfg.get("battery_soc_entity", ""))
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
        now = utcnow()
        for slot in plan.slots:
            if slot.start <= now < slot.end:
                return _map_action(slot.action)
        return "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.coordinator.plan
        if not plan:
            return {}
        return {
            "slots": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "price": s.price,
                    "solar_kwh": s.solar_kwh,
                    "action": _map_action(s.action),
                    "action_kw": round(s.action_kw, 3),
                }
                for s in plan.slots
            ],
            "initial_soc_kwh": plan.initial_soc_kwh,
            "projected_soc_kwh": [round(x, 3) for x in plan.projected_soc],
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
        now = utcnow()
        for slot in plan.slots:
            if slot.start >= now and _map_action(slot.action) != ACTION_HOLD:
                return f"{_map_action(slot.action)} at {slot.start.strftime('%H:%M')}" 
        return ACTION_HOLD

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.coordinator.plan
        if not plan or not plan.slots:
            return {}
        return {
            "current_slot_action": _map_action(plan.slots[0].action) if plan.slots else ACTION_HOLD,
            "current_slot_power_kw": round(plan.slots[0].action_kw, 3) if plan.slots else 0,
        }
