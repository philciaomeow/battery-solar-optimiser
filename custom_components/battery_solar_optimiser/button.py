"""Button platform for Battery Solar Optimiser."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .sensor import BatterySolarOptimiserCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up the button platform."""
    coordinator: BatterySolarOptimiserCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([BatterySolarOptimiserRecalculateButton(coordinator)])


class BatterySolarOptimiserRecalculateButton(ButtonEntity):
    """Button to manually recalculate the optimisation plan."""

    _attr_name = "Recalculate"
    _attr_icon = "mdi:refresh"
    _attr_should_poll = False

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_recalculate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=coordinator.config_entry.title,
            manufacturer="Battery Solar Optimiser",
            model="Battery Optimiser",
        )

    async def async_press(self) -> None:
        """Recalculate the plan immediately."""
        await self.coordinator.async_refresh()
