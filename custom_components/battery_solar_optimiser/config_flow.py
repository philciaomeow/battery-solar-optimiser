"""Config flow for Battery Solar Optimiser."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _schema_defaults(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                "name", default=defaults.get("name", "Battery Solar Optimiser")
            ): str,
            vol.Required(
                "agile_entity", default=defaults.get("agile_entity", "")
            ): str,
            vol.Required(
                "solar_forecast_entity", default=defaults.get("solar_forecast_entity", "")
            ): str,
            vol.Required(
                "battery_soc_entity", default=defaults.get("battery_soc_entity", "")
            ): str,
            vol.Required(
                "inverter_mode_entity", default=defaults.get("inverter_mode_entity", "")
            ): str,
            vol.Required(
                "battery_capacity_kwh",
                default=float(defaults.get("battery_capacity_kwh", 5.0)),
            ): vol.Coerce(float),
            vol.Required(
                "min_soc_kwh",
                default=float(defaults.get("min_soc_kwh", 0.5)),
            ): vol.Coerce(float),
            vol.Required(
                "max_charge_kw", default=float(defaults.get("max_charge_kw", 3.7))
            ): vol.Coerce(float),
            vol.Required(
                "max_discharge_kw", default=float(defaults.get("max_discharge_kw", 3.7))
            ): vol.Coerce(float),
            vol.Required(
                "round_trip_efficiency",
                default=float(defaults.get("round_trip_efficiency", 0.95)),
            ): vol.Coerce(float),
            vol.Required(
                "hourly_load_w", default=float(defaults.get("hourly_load_w", 600))
            ): vol.Coerce(float),
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
            data_schema=_schema_defaults(),
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
            data_schema=_schema_defaults(self.config_entry.data),
        )
