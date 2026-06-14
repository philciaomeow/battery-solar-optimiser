"""Config flow for Battery Solar Optimiser."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


AGILE_ENTITY_PATTERNS = ("octopus_energy", "agile")
SOLAR_ENTITY_PATTERNS = ("forecast_solar", "solcast", "solar_forecast", "pv")
BATTERY_SOC_PATTERNS = ("battery_soc", "battery_state_of_charge", "soc")
INVERTER_MODE_PATTERNS = ("solar_inverter", "inverter_mode", "solax")


def _score_entity(entity_id: str, attrs: dict, patterns: tuple[str, ...]) -> int:
    """Simple scoring for likely entities."""
    score = 0
    entity_id_lower = entity_id.lower()
    for p in patterns:
        if p in entity_id_lower:
            score += 10
    attrs_str = " ".join(str(k) + " " + str(v) for k, v in attrs.items()).lower()
    for p in patterns:
        if p in attrs_str:
            score += 3
    return score


def _guess_entities(hass) -> dict[str, str]:
    """Suggest likely entity IDs from existing Home Assistant entities."""
    best = {"agile_entity": "", "solar_forecast_entity": "", "battery_soc_entity": "", "inverter_mode_entity": ""}
    scores = {"agile_entity": 0, "solar_forecast_entity": 0, "battery_soc_entity": 0, "inverter_mode_entity": 0}
    for state in hass.states.async_all():
        entity_id = state.entity_id
        attrs = state.attributes
        for key, patterns in (
            ("agile_entity", AGILE_ENTITY_PATTERNS),
            ("solar_forecast_entity", SOLAR_ENTITY_PATTERNS),
            ("battery_soc_entity", BATTERY_SOC_PATTERNS),
            ("inverter_mode_entity", INVERTER_MODE_PATTERNS),
        ):
            s = _score_entity(entity_id, attrs, patterns)
            if s > scores[key]:
                scores[key] = s
                best[key] = entity_id
    return best


def _schema(
    hass,
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    defaults = defaults or {}
    guesses = _guess_entities(hass)
    return vol.Schema(
        {
            vol.Required(
                "name",
                default=defaults.get("name", "Battery Solar Optimiser"),
            ): TextSelector(TextSelectorConfig(type="text")),
            vol.Required(
                "agile_entity",
                default=defaults.get("agile_entity", guesses.get("agile_entity", "")),
            ): EntitySelector(EntitySelectorConfig(domain=["event", "sensor"])),
            vol.Optional(
                "solar_forecast_entity",
                default=defaults.get("solar_forecast_entity", guesses.get("solar_forecast_entity", "")),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Required(
                "battery_soc_entity",
                default=defaults.get("battery_soc_entity", guesses.get("battery_soc_entity", "")),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                "inverter_mode_entity",
                default=defaults.get("inverter_mode_entity", guesses.get("inverter_mode_entity", "")),
            ): EntitySelector(EntitySelectorConfig(domain=["select", "sensor"])),
            vol.Required(
                "battery_capacity_kwh",
                default=float(defaults.get("battery_capacity_kwh", 5.0)),
            ): NumberSelector(NumberSelectorConfig(min=0, max=100, step=0.1, unit_of_measurement="kWh")),
            vol.Required(
                "min_soc_kwh",
                default=float(defaults.get("min_soc_kwh", 0.5)),
            ): NumberSelector(NumberSelectorConfig(min=0, max=100, step=0.1, unit_of_measurement="kWh")),
            vol.Required(
                "pv_capacity_kwh",
                default=float(defaults.get("pv_capacity_kwh", 5.0)),
            ): NumberSelector(NumberSelectorConfig(min=0, max=100, step=0.1, unit_of_measurement="kWh")),
            vol.Required(
                "hourly_load_w",
                default=float(defaults.get("hourly_load_w", 600)),
            ): NumberSelector(NumberSelectorConfig(min=0, max=20000, step=10, unit_of_measurement="W")),
            vol.Required(
                "max_charge_kw",
                default=float(defaults.get("max_charge_kw", 3.7)),
            ): NumberSelector(NumberSelectorConfig(min=0, max=50, step=0.1, unit_of_measurement="kW")),
            vol.Required(
                "max_discharge_kw",
                default=float(defaults.get("max_discharge_kw", 3.7)),
            ): NumberSelector(NumberSelectorConfig(min=0, max=50, step=0.1, unit_of_measurement="kW")),
            vol.Required(
                "round_trip_efficiency",
                default=float(defaults.get("round_trip_efficiency", 0.95)),
            ): NumberSelector(NumberSelectorConfig(min=0.5, max=1.0, step=0.01)),
        }
    )


class BatterySolarOptimiserConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            title = user_input.get("name", "Battery Solar Optimiser")
            return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(self.hass),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return BatterySolarOptimiserOptionsFlow(config_entry)


class BatterySolarOptimiserOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=_schema(self.hass, self.config_entry.data),
        )
