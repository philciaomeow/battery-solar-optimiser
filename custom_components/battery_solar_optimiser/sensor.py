"""Sensor platform for Battery Solar Optimiser."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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
from homeassistant.const import UnitOfEnergy

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


def _to_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _align_to_half_hour(value: datetime) -> datetime:
    """Round a datetime down to the current half-hour boundary."""
    if value.minute < 30:
        return value.replace(minute=0, second=0, microsecond=0)
    return value.replace(minute=30, second=0, microsecond=0)


def _local_time(value: datetime, time_zone: str = "Europe/London") -> str:
    """Format a datetime in the configured display timezone."""
    try:
        tz = ZoneInfo(time_zone or "Europe/London")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Europe/London")
    return value.astimezone(tz).strftime("%H:%M")


def _extract_rates_from_state(state) -> list[tuple[datetime, float]]:
    """Extract Octopus Energy rates as (start, GBP/kWh) pairs from a HA state."""
    rates: list[tuple[datetime, float]] = []
    if state and isinstance(state.attributes.get("rates"), list):
        for rate in state.attributes["rates"]:
            try:
                start = _parse_dt(rate.get("start"))
                if start is None:
                    continue
                price = float(rate["value_inc_vat"])
                rates.append((start, price))
            except (KeyError, ValueError, TypeError):
                continue
    return rates


def _previous_day_entity_id(entity_id: str) -> str | None:
    """Infer Octopus previous-day rates entity from current-day rates entity."""
    if "current_day_rates" in entity_id:
        return entity_id.replace("current_day_rates", "previous_day_rates")
    return None


def _next_day_entity_id(entity_id: str) -> str | None:
    """Infer Octopus next-day rates entity from current-day rates entity."""
    if "current_day_rates" in entity_id:
        return entity_id.replace("current_day_rates", "next_day_rates")
    return None


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


def _state_float(state) -> float | None:
    """Return a state's float value, or None when unavailable."""
    if state is None or state.state in (None, "unavailable", "unknown"):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _power_state_to_slot_kwh(state) -> float | None:
    """Convert a W/kW power state to kWh for one 30-minute slot."""
    value = _state_float(state)
    if value is None:
        return None
    uom = str(state.attributes.get("unit_of_measurement", "")).lower()
    if uom == "w":
        return max(0.0, value / 1000.0 * 0.5)
    if uom == "kw":
        return max(0.0, value * 0.5)
    return None


def _power_state_to_watts(state) -> float | None:
    """Convert a W/kW power state to watts."""
    value = _state_float(state)
    if value is None:
        return None
    uom = str(state.attributes.get("unit_of_measurement", "")).lower()
    if uom == "w":
        return max(0.0, value)
    if uom == "kw":
        return max(0.0, value * 1000.0)
    return None


def _energy_state_to_half_hour_kwh(state) -> float | None:
    """Convert an hourly kWh energy state to a half-hour slot estimate."""
    value = _state_float(state)
    if value is None:
        return None
    uom = str(state.attributes.get("unit_of_measurement", "")).lower()
    if uom == "kwh":
        return max(0.0, value / 2.0)
    if uom == "wh":
        return max(0.0, value / 1000.0 / 2.0)
    return None


def _energy_state_kwh(state) -> float | None:
    """Convert a total energy sensor state to kWh."""
    value = _state_float(state)
    if value is None:
        return None
    uom = str(state.attributes.get("unit_of_measurement", "")).lower()
    if uom == "kwh":
        return max(0.0, value)
    if uom == "wh":
        return max(0.0, value / 1000.0)
    return None


def _solar_from_power_sensors(state_api, solar_entity: str, now: datetime) -> list[tuple[datetime, float]]:
    """Build a 48-slot solar forecast from Forecast.Solar-style sensors.

    Forecast.Solar exposes power sensors for now/next hour and energy sensors
    such as ``energy_production_today_remaining`` and
    ``energy_production_tomorrow``. The energy sensors are totals, not per-hour
    readings, so distribute them across daylight half-hour slots instead of
    dividing by two. That keeps the dashboard forecast in the same order of
    magnitude as the daily kWh forecast.
    """
    selected = state_api.get(solar_entity)
    current_slot = _power_state_to_slot_kwh(selected) or _energy_state_to_half_hour_kwh(selected)

    next_hour = None
    current_hour_energy = None
    next_hour_energy = None
    today_remaining = None
    tomorrow_total = None
    if solar_entity.endswith("power_production_now"):
        prefix = solar_entity[: -len("power_production_now")]
        next_hour = _power_state_to_slot_kwh(state_api.get(f"{prefix}power_production_next_hour"))
        current_hour_energy = _energy_state_to_half_hour_kwh(state_api.get(f"{prefix}energy_current_hour"))
        next_hour_energy = _energy_state_to_half_hour_kwh(state_api.get(f"{prefix}energy_next_hour"))
        today_remaining = _energy_state_kwh(state_api.get(f"{prefix}energy_production_today_remaining"))
        tomorrow_total = _energy_state_kwh(state_api.get(f"{prefix}energy_production_tomorrow"))

    if current_hour_energy is not None:
        current_slot = current_hour_energy
    if next_hour_energy is not None:
        next_hour = next_hour_energy

    if current_slot is None and next_hour is None and today_remaining is None and tomorrow_total is None:
        return []

    base = _align_to_half_hour(now)
    slots = [(base + timedelta(minutes=30 * i), 0.0) for i in range(48)]

    # Seed near-term power/energy where Forecast.Solar provides it.
    for i, (slot_start, val) in enumerate(slots):
        if i < 2 and current_slot is not None:
            slots[i] = (slot_start, current_slot)
        elif i < 4 and next_hour is not None:
            slots[i] = (slot_start, next_hour)

    try:
        local_tz = ZoneInfo("Europe/London")
    except ZoneInfoNotFoundError:  # pragma: no cover - tzdata should exist in HA
        local_tz = timezone.utc
    now_local = now.astimezone(local_tz)

    def _daylight_weight(slot_start: datetime) -> float:
        hour = slot_start.astimezone(local_tz).hour + slot_start.astimezone(local_tz).minute / 60.0
        sunrise = 5.0
        sunset = 21.5
        peak = 13.0
        if hour < sunrise or hour > sunset:
            return 0.0
        return max(0.05, 1.0 - abs(hour - peak) / ((sunset - sunrise) / 2.0))

    def _distribute(total_kwh: float | None, target_date) -> None:
        if total_kwh is None or total_kwh <= 0:
            return
        indexes = [
            i for i, (slot_start, _val) in enumerate(slots)
            if slot_start.astimezone(local_tz).date() == target_date and slot_start >= now
        ]
        weights = [_daylight_weight(slots[i][0]) for i in indexes]
        weight_total = sum(weights)
        if weight_total <= 0:
            return
        for i, weight in zip(indexes, weights, strict=False):
            slot_start, existing = slots[i]
            distributed = total_kwh * (weight / weight_total)
            # Keep explicit near-term readings if they are higher, otherwise the
            # total-energy distribution provides the broader daily shape.
            slots[i] = (slot_start, max(existing, distributed))

    _distribute(today_remaining, now_local.date())
    _distribute(tomorrow_total, now_local.date() + timedelta(days=1))
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
        BatterySolarOptimiserAverageHouseLoadSensor(coordinator),
    ]
    coordinator.entities.extend(entities)

    async_add_entities(entities)

    # Refresh when the configured source entities become available or change.
    cfg = {**config_entry.data, **config_entry.options}
    next_day_entity = cfg.get("next_day_rates_entity") or _next_day_entity_id(cfg.get("agile_entity", ""))
    source_entities = [
        cfg.get("agile_entity"),
        cfg.get("previous_day_rates_entity"),
        next_day_entity,
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
            "slot_overrides": {},
            "controls": {},
        }
        self.plan: Plan | None = None
        self.entities: list[SensorEntity] = []
        self._listeners = []
        self._refreshing = False

    def _write_entity_states(self) -> None:
        """Push coordinator state to all registered entities."""
        for entity in self.entities:
            if entity.hass:
                entity.async_write_ha_state()

    def _slot_override_key(self, slot_index: int) -> str | None:
        """Return the absolute start-time key for a visible relative slot."""
        if not self.plan or slot_index >= len(self.plan.slots):
            return None
        return self.plan.slots[slot_index].start.isoformat()

    def set_slot_override(self, slot_index: int, action: str | None) -> None:
        """Store an override for the absolute time represented by a relative slot."""
        key = self._slot_override_key(slot_index)
        if key is None:
            return
        overrides = dict(self.data.get("slot_overrides", {}))
        if action in ("charge", "discharge"):
            overrides[key] = action
        else:
            overrides.pop(key, None)
        self.data["slot_overrides"] = overrides

    def get_slot_override(self, slot_index: int) -> str | None:
        """Return the internal override action for a relative plan slot."""
        key = self._slot_override_key(slot_index)
        if key is None:
            return None
        return dict(self.data.get("slot_overrides", {})).get(key)

    def slot_overrides_for_starts(self, starts: list[datetime]) -> dict[int, str]:
        """Return relative-index overrides for a new rolling plan and prune old ones."""
        overrides = dict(self.data.get("slot_overrides", {}))
        start_keys = {slot_start.isoformat(): idx for idx, slot_start in enumerate(starts)}
        active: dict[int, str] = {}
        pruned: dict[str, str] = {}
        for key, action in overrides.items():
            if key in start_keys and action in ("charge", "discharge"):
                active[start_keys[key]] = action
                pruned[key] = action
        if pruned != overrides:
            self.data["slot_overrides"] = pruned
        return active

    def set_control_value(self, key: str, value: float) -> None:
        """Store a live tuning control value."""
        controls = dict(self.data.get("controls", {}))
        controls[key] = float(value)
        self.data["controls"] = controls

    def get_control_value(self, key: str, default: float) -> float:
        """Return a live tuning control value."""
        try:
            return float(dict(self.data.get("controls", {})).get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _effective_min_soc_kwh(self, cfg: dict[str, Any]) -> float:
        """Return the live minimum reserve converted from percentage to kWh."""
        capacity = float(cfg.get("battery_capacity_kwh", 5.0)) or 5.0
        configured_min = float(cfg.get("min_soc_kwh", 0.5))
        configured_percent = (configured_min / capacity) * 100.0
        reserve_percent = self.get_control_value("min_reserve_percent", max(20.0, configured_percent))
        return max(0.0, min(capacity, capacity * reserve_percent / 100.0))

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

    def _default_load_power_entity(self) -> str:
        """Infer a likely house-load power sensor if one is not configured."""
        preferred = (
            "sensor.solax_house_load",
            "sensor.solax_energy_dashboard_solax_home_consumption_power",
            "sensor.solax_inverter_power",
        )
        for entity_id in preferred:
            if self.hass.states.get(entity_id):
                return entity_id
        for state in self.hass.states.async_all("sensor"):
            entity_id = state.entity_id.lower()
            friendly = str(state.attributes.get("friendly_name", "")).lower()
            uom = str(state.attributes.get("unit_of_measurement", "")).lower()
            if uom in ("w", "kw") and ("house_load" in entity_id or "home_consumption" in entity_id or "house load" in friendly):
                return state.entity_id
        return ""

    def _load_power_entity(self, cfg: dict[str, Any]) -> str:
        """Return configured/inferred live house-load power sensor."""
        return str(cfg.get("load_power_entity") or self._default_load_power_entity())

    def _average_watts_from_states(self, states: list[Any], start_time: datetime, end_time: datetime) -> float | None:
        """Calculate a time-weighted average W from recorder states."""
        samples: list[tuple[datetime, float]] = []
        for state in states:
            watts = _power_state_to_watts(state)
            if watts is None:
                continue
            ts = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
            if ts is None:
                continue
            samples.append((_to_utc(ts), watts))
        if not samples:
            return None
        samples.sort(key=lambda item: item[0])
        total_wh = 0.0
        total_hours = 0.0
        last_ts = start_time
        last_watts = samples[0][1]
        for ts, watts in samples:
            ts = max(start_time, min(ts, end_time))
            if ts > last_ts:
                hours = (ts - last_ts).total_seconds() / 3600.0
                total_wh += last_watts * hours
                total_hours += hours
            last_ts = ts
            last_watts = watts
        if end_time > last_ts:
            hours = (end_time - last_ts).total_seconds() / 3600.0
            total_wh += last_watts * hours
            total_hours += hours
        if total_hours <= 0:
            return samples[-1][1]
        return max(0.0, total_wh / total_hours)

    async def _calculate_average_house_load_w(self, cfg: dict[str, Any], now: datetime) -> float | None:
        """Calculate average house load from recorder history."""
        entity_id = self._load_power_entity(cfg)
        hours = int(self.get_control_value("house_load_average_hours", 24))
        self.data["house_load_average_period_hours"] = hours
        self.data["house_load_entity"] = entity_id or None
        if not entity_id:
            return None
        start_time = now - timedelta(hours=hours)
        end_time = now

        def _fetch_states():
            from homeassistant.components.recorder import history as recorder_history

            return recorder_history.get_significant_states(
                self.hass,
                start_time,
                end_time,
                entity_ids=[entity_id],
                significant_changes_only=False,
                minimal_response=False,
                no_attributes=False,
            ).get(entity_id, [])

        try:
            from homeassistant.components.recorder import get_instance

            states = await get_instance(self.hass).async_add_executor_job(_fetch_states)
        except Exception as err:  # pragma: no cover - recorder availability varies in tests
            _LOGGER.warning("Could not calculate average house load from %s: %s", entity_id, err)
            return None
        average_w = self._average_watts_from_states(states, start_time, end_time)
        if average_w is None:
            current_w = _power_state_to_watts(self.hass.states.get(entity_id))
            average_w = current_w
        return average_w

    async def _effective_load_w(self, cfg: dict[str, Any], now: datetime) -> float:
        """Return load W used for planning and store display metadata."""
        manual_w = self.get_control_value("manual_house_load_w", float(cfg.get("hourly_load_w", 600)))
        use_average = bool(self.get_control_value("use_average_house_load", 1.0))
        average_w = await self._calculate_average_house_load_w(cfg, now) if use_average else None
        effective_w = average_w if average_w is not None else manual_w
        self.data["manual_house_load_w"] = manual_w
        self.data["average_house_load_w"] = average_w
        self.data["effective_house_load_w"] = effective_w
        self.data["use_average_house_load"] = use_average
        return float(effective_w)

    async def _refresh_rate_entities(self, cfg: dict[str, Any]) -> None:
        """Ask Home Assistant to refresh Octopus rate entities before planning.

        Octopus publishes Agile rates through event entities. The optimiser was
        previously recalculating from whatever happened to be cached in HA, so a
        manual/scheduled BSO refresh could still use stale rates. Force-refresh
        the current, next-day, and previous-day rate entities first; then read
        their updated attributes for the plan.
        """
        agile_entity = cfg.get("agile_entity", "")
        previous_day_entity = cfg.get("previous_day_rates_entity") or _previous_day_entity_id(agile_entity) or ""
        next_day_entity = cfg.get("next_day_rates_entity") or _next_day_entity_id(agile_entity) or ""
        entity_ids = [e for e in dict.fromkeys([agile_entity, next_day_entity, previous_day_entity]) if e]
        if not entity_ids:
            return
        try:
            await self.hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": entity_ids},
                blocking=True,
            )
        except Exception as err:  # pragma: no cover - defensive against HA service failures
            _LOGGER.warning("Could not refresh Agile rate entities %s: %s", entity_ids, err)

    async def async_refresh(self, now: datetime | None = None) -> None:
        """Refresh optimisation plan and push updates to entities."""
        if self._refreshing:
            return
        self._refreshing = True
        cfg = self.cfg
        soc_entity = cfg.get("battery_soc_entity", "")
        refresh_started = dt_util.utcnow()
        self.data["status"] = "running"
        self._write_entity_states()
        try:
            await self._refresh_rate_entities(cfg)

            state_api = self.hass.states
            effective_min_soc_kwh = self._effective_min_soc_kwh(cfg)
            soc_state = state_api.get(soc_entity)
            try:
                if soc_state is None or soc_state.state in (None, "unavailable", "unknown"):
                    current_soc_kwh = effective_min_soc_kwh
                else:
                    raw_soc = float(soc_state.state)
                    uom = soc_state.attributes.get("unit_of_measurement", "").lower()
                    if uom == "%":
                        # Convert percentage to kWh using configured battery capacity
                        current_soc_kwh = raw_soc / 100.0 * cfg.get("battery_capacity_kwh", 5.0)
                    else:
                        current_soc_kwh = raw_soc
            except (ValueError, TypeError):
                current_soc_kwh = effective_min_soc_kwh

            agile_entity = cfg.get("agile_entity", "")
            rates_state = state_api.get(agile_entity)
            agile_rates = _extract_rates_from_state(rates_state)

            next_day_entity = cfg.get("next_day_rates_entity") or _next_day_entity_id(agile_entity) or ""
            next_day_rates = _extract_rates_from_state(state_api.get(next_day_entity))
            agile_rates = [*agile_rates, *next_day_rates]

            previous_day_entity = cfg.get("previous_day_rates_entity") or _previous_day_entity_id(agile_entity) or ""
            previous_day_rates = _extract_rates_from_state(state_api.get(previous_day_entity))
            historical_rates = [*previous_day_rates, *agile_rates]

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

            # If no detailed forecast exists, try Forecast.Solar style power/energy
            # sensors and merge known near-term readings over the generic bell curve.
            if not solar_forecast:
                heuristic = _solar_heuristic(refresh_started, float(cfg.get("pv_capacity_kwh", 5.0)))
                power_sensor_forecast = _solar_from_power_sensors(state_api, solar_entity, refresh_started)
                if power_sensor_forecast:
                    solar_forecast = power_sensor_forecast
                else:
                    solar_forecast = heuristic

            discharge_aggressiveness = self.get_control_value("discharge_aggressiveness", 60.0)
            charge_rate_kw = self.get_control_value("charge_rate_kw", float(cfg.get("max_charge_kw", 3.7)))
            effective_load_w = await self._effective_load_w(cfg, refresh_started)
            low_soc = current_soc_kwh < effective_min_soc_kwh
            first_slot = _align_to_half_hour(refresh_started)
            plan_starts = [first_slot + timedelta(minutes=30 * i) for i in range(48)]
            slot_overrides = self.slot_overrides_for_starts(plan_starts)
            self.plan = build_plan(
                now=refresh_started,
                agile_rates=agile_rates,
                solar_forecast=solar_forecast,
                battery_capacity_kwh=float(cfg.get("battery_capacity_kwh", 5.0)),
                min_soc_kwh=effective_min_soc_kwh,
                current_soc_kwh=current_soc_kwh,
                load_w=effective_load_w,
                max_charge_kw=charge_rate_kw,
                max_discharge_kw=float(cfg.get("max_discharge_kw", 3.7)),
                efficiency=float(cfg.get("round_trip_efficiency", 0.95)),
                missing_rate_pence=float(cfg.get("missing_rate_pence", 30.0)),
                previous_day_rates=previous_day_rates,
                historical_rates=historical_rates,
                lookback_hours=int(float(cfg.get("lookback_hours", 12))),
                slot_overrides=slot_overrides,
                discharge_aggressiveness=discharge_aggressiveness,
            )

            source_counts = {"actual": 0, "previous_day": 0, "fallback": 0}
            if self.plan:
                for slot in self.plan.slots:
                    source_counts[slot.price_source] = source_counts.get(slot.price_source, 0) + 1
            self.data["price_source_counts"] = source_counts
            self.data["previous_day_rates_entity"] = previous_day_entity or None
            self.data["next_day_rates_entity"] = next_day_entity or None
            self.data["effective_min_soc_kwh"] = effective_min_soc_kwh
            self.data["discharge_aggressiveness"] = discharge_aggressiveness
            self.data["charge_rate_kw"] = charge_rate_kw

            refresh_finished = dt_util.utcnow()
            self.data["last_updated"] = refresh_finished
            self.data["next_update_due"] = self._next_scheduled_refresh(refresh_finished, low_soc)
            self.data["status"] = "idle"
            self._write_entity_states()
        finally:
            if self.data.get("status") == "running":
                self.data["status"] = "idle"
                self._write_entity_states()
            self._refreshing = False

        # Schedule regular and low-SOC refreshes once, when first entity is added.
        if not self._listeners:
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
                except (AttributeError, ValueError, TypeError):
                    return
                if current_soc < self._effective_min_soc_kwh(self.cfg):
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
                "price_source": s.price_source,
                "solar_kwh": round(s.solar_kwh, 3),
                "action": _map_action(s.action),
                "override": self.coordinator.get_slot_override(idx) or "none",
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
            "price_source_counts": self.coordinator.data.get("price_source_counts", {}),
            "previous_day_rates_entity": self.coordinator.data.get("previous_day_rates_entity"),
            "next_day_rates_entity": self.coordinator.data.get("next_day_rates_entity"),
            "minimum_reserve_kwh": round(float(self.coordinator.data.get("effective_min_soc_kwh", 0.0)), 3),
            "discharge_aggressiveness": self.coordinator.data.get("discharge_aggressiveness"),
            "charge_rate_kw": self.coordinator.data.get("charge_rate_kw"),
            "effective_house_load_w": round(float(self.coordinator.data.get("effective_house_load_w", 0.0)), 1),
            "average_house_load_w": round(float(self.coordinator.data.get("average_house_load_w") or 0.0), 1),
            "manual_house_load_w": round(float(self.coordinator.data.get("manual_house_load_w", 0.0)), 1),
            "use_average_house_load": self.coordinator.data.get("use_average_house_load"),
            "house_load_average_period_hours": self.coordinator.data.get("house_load_average_period_hours"),
            "house_load_entity": self.coordinator.data.get("house_load_entity"),
            "lookback_hours": int(float(self.coordinator.cfg.get("lookback_hours", 12))),
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


class BatterySolarOptimiserAverageHouseLoadSensor(BatterySolarOptimiserBaseSensor):
    """Sensor showing calculated/effective average house load."""

    _attr_name = "Average House Load"
    _attr_icon = "mdi:home-lightning-bolt"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator: BatterySolarOptimiserCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_average_house_load"

    @property
    def native_value(self) -> float | None:
        watts = self.coordinator.data.get("average_house_load_w")
        if watts is None:
            watts = self.coordinator.data.get("effective_house_load_w")
        try:
            # kWh per hour is numerically kW; this matches the planning load input.
            return round(float(watts) / 1000.0, 3)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        average_w = self.coordinator.data.get("average_house_load_w")
        effective_w = self.coordinator.data.get("effective_house_load_w")
        manual_w = self.coordinator.data.get("manual_house_load_w")
        return {
            "average_w": round(float(average_w), 1) if average_w is not None else None,
            "effective_w": round(float(effective_w), 1) if effective_w is not None else None,
            "manual_w": round(float(manual_w), 1) if manual_w is not None else None,
            "period_hours": self.coordinator.data.get("house_load_average_period_hours"),
            "source_entity": self.coordinator.data.get("house_load_entity"),
            "use_average": self.coordinator.data.get("use_average_house_load"),
        }
