"""Number platform for Battery Solar Optimiser live tuning controls."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .sensor import BatterySolarOptimiserCoordinator

CONTROL_MIN_RESERVE_PERCENT = "min_reserve_percent"
CONTROL_DISCHARGE_AGGRESSIVENESS = "discharge_aggressiveness"
CONTROL_MANUAL_HOUSE_LOAD_W = "manual_house_load_w"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up the number platform."""
    coordinator: BatterySolarOptimiserCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities = [
        BatterySolarOptimiserMinReserveNumber(coordinator),
        BatterySolarOptimiserDischargeAggressivenessNumber(coordinator),
        BatterySolarOptimiserManualHouseLoadNumber(coordinator),
    ]
    coordinator.entities.extend(entities)
    async_add_entities(entities)


class BatterySolarOptimiserBaseNumber(NumberEntity, RestoreEntity):
    """Base live tuning number."""

    _attr_should_poll = False
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = NumberDeviceClass.BATTERY

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=coordinator.config_entry.title,
            manufacturer="Battery Solar Optimiser",
            model="Battery Optimiser",
        )

    @property
    def native_value(self) -> float | None:
        return self.coordinator.get_control_value(self.control_key, self.recommended_value)

    @property
    def extra_state_attributes(self) -> dict[str, float | str]:
        return {
            "recommended_value": self.recommended_value,
            "recommended_display": self.recommended_display,
            "description": self.description,
        }

    @property
    def recommended_display(self) -> str:
        return f"{self.recommended_value:g}%"

    async def async_added_to_hass(self) -> None:
        """Restore the live tuning value after HA restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self.coordinator.set_control_value(self.control_key, float(last_state.state))
            except ValueError:
                self.coordinator.set_control_value(self.control_key, self.recommended_value)
        else:
            self.coordinator.set_control_value(self.control_key, self.recommended_value)

    async def async_set_native_value(self, value: float) -> None:
        """Update a live tuning value and recalculate the plan."""
        value = max(float(self._attr_native_min_value), min(float(self._attr_native_max_value), float(value)))
        self.coordinator.set_control_value(self.control_key, value)
        self.async_write_ha_state()
        await self.coordinator.async_refresh()


class BatterySolarOptimiserMinReserveNumber(BatterySolarOptimiserBaseNumber):
    """Minimum battery reserve percentage used as the discharge floor."""

    control_key = CONTROL_MIN_RESERVE_PERCENT
    recommended_value = 20.0
    description = "Lowest battery percentage the optimiser should plan down to. Lower allows deeper discharge."
    _attr_name = "Minimum Reserve"
    _attr_icon = "mdi:battery-lock"
    _attr_native_min_value = 5.0
    _attr_native_max_value = 80.0
    _attr_native_step = 1.0

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_minimum_reserve_percent"


class BatterySolarOptimiserDischargeAggressivenessNumber(BatterySolarOptimiserBaseNumber):
    """How readily the optimiser treats slots as expensive enough to discharge."""

    control_key = CONTROL_DISCHARGE_AGGRESSIVENESS
    recommended_value = 60.0
    description = "Higher means discharge in more moderately-expensive slots; lower preserves battery for only the worst slots."
    _attr_name = "Discharge Aggressiveness"
    _attr_icon = "mdi:battery-arrow-down"
    _attr_device_class = None
    _attr_native_min_value = 0.0
    _attr_native_max_value = 100.0
    _attr_native_step = 5.0

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_discharge_aggressiveness"


class BatterySolarOptimiserManualHouseLoadNumber(BatterySolarOptimiserBaseNumber):
    """Manual average house load fallback when history averaging is disabled/unavailable."""

    control_key = CONTROL_MANUAL_HOUSE_LOAD_W
    recommended_value = 600.0
    description = "Manual average house load used when calculated averaging is disabled or unavailable."
    _attr_name = "Manual House Load"
    _attr_icon = "mdi:home-lightning-bolt"
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 0.0
    _attr_native_max_value = 5000.0
    _attr_native_step = 10.0
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_manual_house_load_w"

    @property
    def recommended_display(self) -> str:
        return f"{self.recommended_value:g} W"
