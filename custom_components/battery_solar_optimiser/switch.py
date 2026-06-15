"""Switch platform for Battery Solar Optimiser live tuning controls."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .sensor import BatterySolarOptimiserCoordinator

CONTROL_USE_AVERAGE_HOUSE_LOAD = "use_average_house_load"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up the switch platform."""
    coordinator: BatterySolarOptimiserCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entity = BatterySolarOptimiserUseAverageHouseLoadSwitch(coordinator)
    coordinator.entities.append(entity)
    async_add_entities([entity])


class BatterySolarOptimiserUseAverageHouseLoadSwitch(SwitchEntity, RestoreEntity):
    """Toggle use of calculated average house load for planning."""

    _attr_name = "Use Average House Load"
    _attr_icon = "mdi:chart-bell-curve"
    _attr_should_poll = False

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_use_average_house_load"
        self._attr_is_on = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=coordinator.config_entry.title,
            manufacturer="Battery Solar Optimiser",
            model="Battery Optimiser",
        )

    async def async_added_to_hass(self) -> None:
        """Restore the toggle after HA restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        self._attr_is_on = False if last_state and last_state.state == "off" else True
        self.coordinator.set_control_value(CONTROL_USE_AVERAGE_HOUSE_LOAD, 1.0 if self._attr_is_on else 0.0)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.get_control_value(CONTROL_USE_AVERAGE_HOUSE_LOAD, 1.0))

    async def async_turn_on(self, **kwargs) -> None:
        """Enable calculated average house load."""
        self.coordinator.set_control_value(CONTROL_USE_AVERAGE_HOUSE_LOAD, 1.0)
        self.async_write_ha_state()
        self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        """Use manual house load instead."""
        self.coordinator.set_control_value(CONTROL_USE_AVERAGE_HOUSE_LOAD, 0.0)
        self.async_write_ha_state()
        self.coordinator.async_request_refresh()
