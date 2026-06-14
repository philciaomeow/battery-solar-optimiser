"""Sensor platform for Battery Solar Optimiser."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.restore import RestoreEntity
from homeassistant.util.dt import utcnow

from .const import (
    ACTION_CHARGING,
    ACTION_DISCHARGING,
    ACTION_HOLD,
    DOMAIN,
)
from .optimiser import Plan, build_plan

_LOGGER = logging.getLogger(__name__)

SENSOR_ICONS = {
    ACTION_CHARGING: "mdi:battery-charging",
    ACTION_DISCHARGING: "mdi:battery-minus",
    ACTION_HOLD: "mdi:battery",
}


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
    coordinator = BatterySolarOptimiserCoordinator(hass, config_entry)
    await coordinator.async_refresh()
    async_add_entities(
        [
            BatterySolarOptimiserPlanSensor(coordinator),
            BatterySolarOptimiserCostSensor(coordinator),
            BatterySolarOptimiserNextActionSensor(coordinator),
        ]
    )


class BatterySolarOptimiserCoordinator:
    """Holds config and shared state."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        self.hass = hass
        self.config_entry = config_entry
        self.data: dict[str, Any] = {}
        self.plan: Plan | None = None
        self._listeners = []

    async def async_refresh(self, now: datetime | None = None) -> None:
        """Refresh optimisation plan."""
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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        cfg = self.coordinator.config_entry.data
        min_soc_kwh = float(cfg.get("min_soc_kwh", 0.5))

        # Refresh at :25 and :55 each hour — 5 mins before Agile slot changes
        self.coordinator._listeners.append(
            async_track_time_change(
                self.coordinator.hass,
                self.coordinator.async_refresh,
                minute=[25, 55],
                second=0,
            )
        )

        # Low SOC fast refresh: every 5 minutes if battery is below min_soc
        async def _low_soc_refresh(_now: datetime) -> None:
            soc_state = self.coordinator.hass.states.get(cfg.get("battery_soc_entity", ""))
            try:
                current_soc = float(soc_state.state)
            except (ValueError, TypeError):
                return
            if current_soc < min_soc_kwh:
                await self.coordinator.async_refresh()

        self.coordinator._listeners.append(
            async_track_time_interval(
                self.coordinator.hass,
                _low_soc_refresh,
                timedelta(minutes=5),
            )
        )

        for unsub in self.coordinator._listeners:
            self.async_on_remove(unsub)


class BatterySolarOptimiserPlanSensor(BatterySolarOptimiserBaseSensor):
    """Sensor exposing the current charge/discharge plan."""

    _attr_name = "Plan"
    _attr_icon = "mdi:chart-timeline"
    _attr_state_class = SensorStateClass.MEASUREMENT

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
    _attr_state_class = SensorStateClass.TOTAL

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
