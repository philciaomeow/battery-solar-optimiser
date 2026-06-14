"""Select platform for Battery Solar Optimiser.

Exposes the current action as a select entity so users can build automations
that respond to charging / discharging / hold commands.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore import RestoreEntity
from homeassistant.util.dt import utcnow

from .const import ACTION_CHARGING, ACTION_DISCHARGING, ACTION_HOLD, DOMAIN, SELECT_OPTIONS
from .sensor import BatterySolarOptimiserCoordinator


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
    """Set up the select platform."""
    coordinator = BatterySolarOptimiserCoordinator(hass, config_entry)
    await coordinator.async_refresh()
    async_add_entities([BatterySolarOptimiserActionSelect(coordinator)])


class BatterySolarOptimiserActionSelect(SelectEntity, RestoreEntity):
    """Select entity representing the recommended inverter action."""

    _attr_should_poll = False
    _attr_icon = "mdi:battery-unknown"
    _attr_options = SELECT_OPTIONS

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_name = "Action"
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_action"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=coordinator.config_entry.title,
            manufacturer="Battery Solar Optimiser",
            model="Battery Optimiser",
        )

    @property
    def current_option(self) -> str | None:
        plan = self.coordinator.plan
        if not plan or not plan.slots:
            return ACTION_HOLD
        now = utcnow()
        for slot in plan.slots:
            if slot.start <= now < slot.end:
                return _map_action(slot.action)
        return ACTION_HOLD

    @property
    def icon(self) -> str:
        return {
            ACTION_CHARGING: "mdi:battery-charging",
            ACTION_DISCHARGING: "mdi:battery-minus",
            ACTION_HOLD: "mdi:battery",
        }.get(self.current_option or ACTION_HOLD, "mdi:battery")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Subscribe to coordinator refresh events
        self.async_on_remove(
            self.coordinator.hass.bus.async_listen(
                "state_changed", self._handle_state_changed
            )
        )

    @callback
    def _handle_state_changed(self, event: dict[str, Any]) -> None:
        self.async_write_ha_state()
