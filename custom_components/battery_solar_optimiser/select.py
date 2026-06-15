"""Select platform for Battery Solar Optimiser.

Exposes the current action as a select entity so users can build automations
that respond to charging / discharging / hold commands. Also exposes per-slot
manual override selects for the 48 half-hour planning slots.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import ACTION_CHARGING, ACTION_DISCHARGING, ACTION_HOLD, DOMAIN, SELECT_OPTIONS
from .sensor import BatterySolarOptimiserCoordinator

OVERRIDE_NO_CHANGE = "No change"
OVERRIDE_FORCE_CHARGE = "Force charge"
OVERRIDE_FORCE_DISCHARGE = "Force discharge"
OVERRIDE_OPTIONS = [OVERRIDE_NO_CHANGE, OVERRIDE_FORCE_CHARGE, OVERRIDE_FORCE_DISCHARGE]
LOAD_PERIOD_OPTIONS = ["24 hours", "48 hours", "72 hours"]
LOAD_PERIOD_TO_HOURS = {"24 hours": 24, "48 hours": 48, "72 hours": 72}


def _map_action(action: str) -> str:
    return {
        "charge": ACTION_CHARGING,
        "discharge": ACTION_DISCHARGING,
        "hold": ACTION_HOLD,
    }.get(action, ACTION_HOLD)


def _override_to_internal(option: str | None) -> str | None:
    if option == OVERRIDE_FORCE_CHARGE:
        return "charge"
    if option == OVERRIDE_FORCE_DISCHARGE:
        return "discharge"
    return None


def _internal_to_override(action: str | None) -> str:
    if action == "charge":
        return OVERRIDE_FORCE_CHARGE
    if action == "discharge":
        return OVERRIDE_FORCE_DISCHARGE
    return OVERRIDE_NO_CHANGE


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up the select platform."""
    coordinator: BatterySolarOptimiserCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities = [
        BatterySolarOptimiserActionSelect(coordinator),
        BatterySolarOptimiserLoadAveragePeriodSelect(coordinator),
    ]
    entities.extend(BatterySolarOptimiserSlotOverrideSelect(coordinator, idx) for idx in range(48))
    coordinator.entities.extend(entities)
    async_add_entities(entities)


class BatterySolarOptimiserBaseSelect(SelectEntity, RestoreEntity):
    """Base select entity."""

    _attr_should_poll = False

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=coordinator.config_entry.title,
            manufacturer="Battery Solar Optimiser",
            model="Battery Optimiser",
        )


class BatterySolarOptimiserActionSelect(BatterySolarOptimiserBaseSelect):
    """Select entity representing the recommended inverter action."""

    _attr_icon = "mdi:battery-unknown"
    _attr_options = SELECT_OPTIONS

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Action"
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_action"

    @property
    def current_option(self) -> str | None:
        plan = self.coordinator.plan
        if not plan or not plan.slots:
            return ACTION_HOLD
        now = dt_util.utcnow()
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


class BatterySolarOptimiserLoadAveragePeriodSelect(BatterySolarOptimiserBaseSelect):
    """Select how much history to use for calculated average house load."""

    _attr_icon = "mdi:history"
    _attr_options = LOAD_PERIOD_OPTIONS

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "House Load Average Period"
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_house_load_average_period"
        self._attr_current_option = LOAD_PERIOD_OPTIONS[0]

    async def async_added_to_hass(self) -> None:
        """Restore period after HA restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        option = last_state.state if last_state and last_state.state in LOAD_PERIOD_OPTIONS else LOAD_PERIOD_OPTIONS[0]
        self._attr_current_option = option
        self.coordinator.set_control_value("house_load_average_hours", LOAD_PERIOD_TO_HOURS[option])

    @property
    def current_option(self) -> str | None:
        hours = int(self.coordinator.get_control_value("house_load_average_hours", 24))
        return f"{hours} hours" if hours in (24, 48, 72) else LOAD_PERIOD_OPTIONS[0]

    async def async_select_option(self, option: str) -> None:
        """Set averaging period and recalculate."""
        if option not in LOAD_PERIOD_OPTIONS:
            return
        self._attr_current_option = option
        self.coordinator.set_control_value("house_load_average_hours", LOAD_PERIOD_TO_HOURS[option])
        self.async_write_ha_state()
        self.coordinator.async_request_refresh()


class BatterySolarOptimiserSlotOverrideSelect(BatterySolarOptimiserBaseSelect):
    """Manual override selector for one relative plan slot."""

    _attr_options = OVERRIDE_OPTIONS
    _attr_icon = "mdi:tune-variant"

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator, slot_index: int) -> None:
        super().__init__(coordinator)
        self.slot_index = slot_index
        self._attr_name = f"Slot {slot_index:02d} Override"
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_slot_{slot_index:02d}_override"
        self._attr_current_option = OVERRIDE_NO_CHANGE

    async def async_added_to_hass(self) -> None:
        """Do not restore relative overrides after HA restart.

        Overrides are stored against absolute slot start times in the coordinator,
        so restoring a relative select state would apply it to the wrong slot
        after the 24-hour plan rolls forward.
        """
        await super().async_added_to_hass()
        self._attr_current_option = OVERRIDE_NO_CHANGE

    @property
    def current_option(self) -> str | None:
        return _internal_to_override(self.coordinator.get_slot_override(self.slot_index))

    async def async_select_option(self, option: str) -> None:
        """Set a manual override for this slot and recalculate the plan."""
        if option not in OVERRIDE_OPTIONS:
            return
        self._attr_current_option = option
        self.coordinator.set_slot_override(self.slot_index, _override_to_internal(option))
        self.async_write_ha_state()
        self.coordinator.async_request_refresh()

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        plan = self.coordinator.plan
        slot = plan.slots[self.slot_index] if plan and self.slot_index < len(plan.slots) else None
        return {
            "slot_index": self.slot_index,
            "slot_start": slot.start.isoformat() if slot else None,
            "slot_action": _map_action(slot.action) if slot else None,
        }
