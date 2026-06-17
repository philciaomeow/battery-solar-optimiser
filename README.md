# Battery Solar Optimiser

Battery Solar Optimiser is a lightweight Home Assistant custom integration for planning simple battery charge, discharge, and hold windows from:

- Octopus Agile import prices
- current-day and next-day Octopus Agile import prices, refreshed before each plan
- optional previous-day Agile prices for rare unpublished/fallback slots
- solar forecast / PV estimate
- current battery state of charge
- calculated or manually configured household load
- battery capacity, charge/discharge limits, efficiency, and minimum reserve

It is designed for people who want something simpler and easier to reason about than a full battery optimiser. It does **not** directly control your inverter. Instead, it exposes Home Assistant entities that you can use in your own automations.

> Safety note: this integration gives recommendations. Your inverter automation is still your responsibility. Test with conservative settings before letting it control real charge/discharge behaviour.

---

## What it does

- Builds a 24-hour plan split into 48 half-hour slots.
- Recommends one of three actions for each slot:
  - `charging`
  - `discharging`
  - `hold`
- Refreshes Octopus Agile current-day, next-day, and previous-day rate entities before every optimiser run.
- Uses actual Octopus Agile current-day and next-day rates when available.
- Uses previous-day same-slot Agile prices when future Agile slots are unpublished.
- Falls back to a configurable pessimistic missing-rate price when no tariff data exists.
- Includes recent historical price context so flat-looking prices after cheap periods are not treated as automatically cheap.
- Handles negative Agile prices and will recharge during cheap/negative post-peak periods when there is battery capacity available.
- Uses self-use discharge planning: battery discharge is limited to household demand and does not intentionally export energy.
- Calculates average house load from recent Home Assistant recorder history, with selectable 24/48/72 hour windows and a manual fallback.
- Exposes live tuning controls for minimum reserve, discharge aggressiveness, battery charge rate, and house-load averaging.
- Reads battery SOC as either `%` or `kWh`.
- Protects a configurable minimum SOC.
- Refreshes shortly before Agile slot changes.
- Exposes a manual recalculate button.
- Exposes manual per-slot override controls.
- Provides a ready-made two-tab Lovelace dashboard: a read-only Plan view and an interactive Settings view with slot override controls.

---

## Main entities

| Entity | Type | Purpose |
| --- | --- | --- |
| `sensor.battery_solar_optimiser_plan` | Sensor | Current slot action plus compact 48-slot plan attributes. |
| `sensor.battery_solar_optimiser_estimated_cost` | Sensor | Estimated import cost over the planning horizon. |
| `sensor.battery_solar_optimiser_next_action` | Sensor | Current/next non-hold action and local time window. |
| `sensor.battery_solar_optimiser_status` | Sensor | `idle` or `running`. Useful to see if the optimiser is recalculating. |
| `sensor.battery_solar_optimiser_last_updated` | Timestamp sensor | Last successful plan refresh. |
| `sensor.battery_solar_optimiser_next_update_due` | Timestamp sensor | Next scheduled optimiser refresh. |
| `sensor.battery_solar_optimiser_average_house_load` | Sensor | Calculated/effective average house load used for planning. |
| `select.battery_solar_optimiser_action` | Select | Current effective action: `charging`, `discharging`, or `hold`. Use this in inverter automations. |
| `select.battery_solar_optimiser_house_load_average_period` | Select | Selects how much history to use for house-load averaging: 24, 48, or 72 hours. |
| `switch.battery_solar_optimiser_use_average_house_load` | Switch | Enables calculated house-load averaging. When off, the manual load value is used. |
| `number.battery_solar_optimiser_minimum_reserve` | Number | Live minimum reserve percentage. |
| `number.battery_solar_optimiser_discharge_aggressiveness` | Number | Live tuning for how readily the optimiser discharges during moderately high prices. |
| `number.battery_solar_optimiser_battery_charge_rate` | Number | Live maximum battery charge rate in kW used by the plan. |
| `number.battery_solar_optimiser_manual_house_load` | Number | Manual house-load fallback in watts. |
| `button.battery_solar_optimiser_recalculate` | Button | Manually refreshes the plan immediately. |

### Slot override entities

The integration also creates 48 slot override select entities:

```text
select.battery_solar_optimiser_slot_00_override
select.battery_solar_optimiser_slot_01_override
...
select.battery_solar_optimiser_slot_47_override
```

Each one has:

- `No change`
- `Force charge`
- `Force discharge`

Overrides are applied before the battery/cost simulation, so the projected battery level, plan table, and `select.battery_solar_optimiser_action` all reflect the forced action.

The slot numbers are relative to the current 24-hour plan. Slot `00` is the first half-hour slot in the current plan, slot `47` is the last. Internally overrides are stored against the absolute slot start time, so a future override follows its original time as the rolling plan moves forward and expired overrides are pruned instead of sticking to the next slot after rollover.

---

## Installation

### Option 1: HACS custom repository

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu and choose **Custom repositories**.
3. Add this repository URL:

   ```text
   https://github.com/philciaomeow/battery-solar-optimiser
   ```

4. Select category **Integration**.
5. Install **Battery Solar Optimiser**.
6. Restart Home Assistant.
7. Go to **Settings → Devices & services → Add integration**.
8. Search for **Battery Solar Optimiser** and complete the setup form.

### Option 2: Manual install

1. Download or clone this repository.
2. Copy this folder:

   ```text
   custom_components/battery_solar_optimiser
   ```

   into your Home Assistant config directory:

   ```text
   /config/custom_components/battery_solar_optimiser
   ```

3. Restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration**.
5. Search for **Battery Solar Optimiser** and complete the setup form.

---

## Configuration

The integration is configured through the Home Assistant UI. No YAML is required for the integration itself.

After setup, you can change settings from:

```text
Settings → Devices & services → Battery Solar Optimiser → Configure
```

### Required / useful fields

| Setting | What to choose |
| --- | --- |
| Agile price entity | Octopus Energy current-day Agile rates entity. It should expose a `rates` attribute with start times and `value_inc_vat`. |
| Next-day rates entity | Optional but recommended. Used for the second half of the 24-hour rolling plan when the plan crosses midnight. If left blank, the integration tries to infer it from the current-day entity name. |
| Previous-day rates entity | Optional but recommended. Used for unpublished future Agile slots. If left blank, the integration tries to infer it from the current-day entity name. |
| Solar forecast entity | Forecast.Solar / Solcast-style entity with forecast attributes, or a Forecast.Solar power/energy sensor such as `sensor.power_production_now`. W/kW power sensors are converted to kWh per 30-minute slot. If unavailable, the integration uses a simple PV-capacity fallback estimate. |
| Battery SOC entity | Battery state of charge. Can be `%` or `kWh`. If unavailable, the optimiser falls back safely to minimum SOC. |
| Battery capacity | Usable battery capacity in kWh. |
| Load power entity | Optional live house-load power sensor in W or kW, e.g. `sensor.solax_house_load`. Used for calculated average house load. |
| Minimum SOC | Reserve in kWh. The optimiser will not intentionally discharge below this. |
| PV capacity | Installed PV size / fallback solar estimate basis. |
| Max charge power | Maximum grid/battery charge power in kW. |
| Max discharge power | Maximum battery discharge power in kW. |
| Round-trip efficiency | Battery efficiency, usually around `0.90`–`0.95`. |
| Hourly load | Manual average home load in watts. Used as a fallback when calculated averaging is disabled or recorder history is unavailable. |
| Display timezone | Timezone for dashboard slot labels, e.g. `Europe/London`. |
| Missing-rate fallback | Pence/kWh used when no actual or previous-day rate is available. Defaults to `30p/kWh` to avoid treating unknown slots as free. |
| Lookback hours | Recent historical price context window. Defaults to 12 hours. |

---

## Scheduling

The optimiser refreshes automatically:

- at `:25` and `:55` each hour, five minutes before Agile half-hour slot boundaries
- when configured source entities change
- every 5 minutes while battery SOC is below the configured minimum SOC
- whenever you press `button.battery_solar_optimiser_recalculate`

---

## Ready-made dashboard

This repository includes a ready-made Lovelace dashboard:

```text
dashboards/battery-solar-optimiser-dashboard.yaml
```

It has two views:

- **Plan** — a full-width two-column view with the read-only 24-hour plan table on the left and all graphs on the right. Forced slots are highlighted with an Off/Charge/Discharge override-status column, but this page intentionally has no override controls so it stays calm while graphs update.
- **Settings** — the same 24-hour plan table with per-slot Charge and Discharge override buttons, live tuning controls, source-entity reference rows, and totals.

The dashboard uses two local custom-card resources from this repository:

```text
www/bso-plan-card.js
www/bso-layout-card.js
```

`bso-layout-card.js` holds each view to two responsive full-width columns on wider screens and stacks them on phones. `bso-plan-card.js` renders the 24-hour plan table.

### Dashboard installation

1. Copy the dashboard YAML into your Home Assistant config folder:

   ```text
   /config/dashboards/battery-solar-optimiser-dashboard.yaml
   ```

2. Copy the custom card JavaScript files into Home Assistant's `www` folder:

   ```text
   /config/www/bso-plan-card.js
   /config/www/bso-layout-card.js
   ```

3. Register the custom card resource in `configuration.yaml`.

   If your Home Assistant uses YAML-mode Lovelace, add it under the global `lovelace.resources` section:

   ```yaml
   lovelace:
     resource_mode: yaml
     resources:
       - url: /local/bso-layout-card.js
         type: module
       - url: /local/bso-plan-card.js
         type: module
     dashboards:
       battery-solar-optimiser:
         mode: yaml
         filename: dashboards/battery-solar-optimiser-dashboard.yaml
         title: Battery Solar Optimiser
         icon: mdi:solar-power-variant
         show_in_sidebar: true
   ```

   If you already have a `lovelace:` section, merge the `resources:` and `dashboards:` entries into your existing section rather than creating a second `lovelace:` block.

4. Restart Home Assistant.
5. Open **Battery Solar Optimiser** from the sidebar.
6. If the custom card still shows a red configuration error or override dropdown behaviour seems stale, hard-refresh your browser. Custom card resources are often cached.

### Dashboard dependencies

The included dashboard also uses:

- `apexcharts-card` for graphs

Install it with HACS before using the included dashboard, or remove the graph cards from the YAML.

---

## Example inverter automation

Use `select.battery_solar_optimiser_action` as the single source of truth for the current effective action. Manual slot overrides change this entity when the overridden slot is current.

This example is deliberately generic. Replace the entity IDs and services with the ones for your inverter integration.

```yaml
alias: Battery optimiser control
mode: restart
trigger:
  - platform: state
    entity_id: select.battery_solar_optimiser_action
  - platform: homeassistant
    event: start
action:
  - choose:
      - conditions:
          - condition: state
            entity_id: select.battery_solar_optimiser_action
            state: charging
        sequence:
          - service: select.select_option
            target:
              entity_id: select.my_inverter_mode
            data:
              option: Charge
          - service: number.set_value
            target:
              entity_id: number.my_inverter_charge_power
            data:
              value: 3500

      - conditions:
          - condition: state
            entity_id: select.battery_solar_optimiser_action
            state: discharging
        sequence:
          - service: select.select_option
            target:
              entity_id: select.my_inverter_mode
            data:
              option: Discharge
          - service: number.set_value
            target:
              entity_id: number.my_inverter_discharge_power
            data:
              value: 3500

      - conditions:
          - condition: state
            entity_id: select.battery_solar_optimiser_action
            state: hold
        sequence:
          - service: select.select_option
            target:
              entity_id: select.my_inverter_mode
            data:
              option: Self Use
          - service: number.set_value
            target:
              entity_id: number.my_inverter_charge_power
            data:
              value: 0
          - service: number.set_value
            target:
              entity_id: number.my_inverter_discharge_power
            data:
              value: 0
```

### Automation tips

- Start with low charge/discharge power limits while testing.
- Add inverter-specific safety checks, such as battery minimum SOC, inverter online state, and grid import/export limits.
- Keep your inverter's own safety limits enabled.
- Watch the first few slot changes before relying on unattended operation.

---

## How the optimiser thinks

The core optimiser is intentionally simple and deterministic:

1. Align the planning horizon to half-hour slots.
2. Load Agile prices, previous-day prices, solar forecast, battery SOC, and configured load.
3. Estimate net load for each slot.
4. Use solar to cover load first, then charge the battery where possible.
5. Protect the configured minimum SOC.
6. Charge during cheap slots, especially before expensive periods or during negative prices.
7. Discharge during expensive slots when there is usable battery above reserve.
8. Hold during normal/flat periods to avoid needless battery cycling.
9. Prefer the cheapest upcoming slots for grid charging rather than charging early at merely average rates.
10. Limit planned discharge to predicted household demand in self-use mode; it does not intentionally export battery energy.
11. Apply any manual slot overrides.
12. Simulate projected SOC and cost from the resulting actions.

It is not a mathematical optimiser and does not use an LP/MILP solver. That is deliberate: the aim is a transparent, dependency-light controller that is easy to understand and debug.

---

## Troubleshooting

### The dashboard shows a red custom card configuration error

Check that the custom card file is available:

```text
/local/bso-plan-card.js
```

Then check it is registered under global Lovelace resources in `configuration.yaml`, not only inside an individual dashboard YAML file.

Restart Home Assistant and hard-refresh your browser.

### The plan has fallback prices

The plan attributes include price source counts. If many slots use fallback, check your Octopus current-day, next-day, and previous-day rates entities. The optimiser asks Home Assistant to refresh those entities before each recalculation, but it can only use data exposed by the Octopus integration.

### The plan charges on a rate that looks too high

Check the neighbouring future slots and the `number.battery_solar_optimiser_battery_charge_rate` value. The optimiser now prefers the cheapest upcoming charge slots and reports `hold` when the battery is already full rather than showing a misleading zero-power charge. If the physical inverter charges slower than the configured value, lower the battery charge-rate number so the plan allocates enough cheap slots to finish charging.

### Override dropdown closes immediately or an override sticks after rollover

Make sure `/local/bso-plan-card.js` is the latest version and hard-refresh the browser. The card avoids re-rendering while a native override select is open, and overrides are tied to absolute slot start times so expired overrides are removed as the plan rolls forward.

If the override dropdown still flashes closed briefly, hard-refresh the browser to pick up the latest card. The current card suppresses plan-table re-rendering for a short interaction window on focus, mouse, and touch events; interactive control changes also wait 30 seconds before recalculating, so rapid changes coalesce into one refresh.

### The battery does not discharge as much as expected

The optimiser does not plan export. In self-use mode, discharge is limited by predicted house load. Check:

- `sensor.battery_solar_optimiser_average_house_load`
- `switch.battery_solar_optimiser_use_average_house_load`
- `select.battery_solar_optimiser_house_load_average_period`
- `number.battery_solar_optimiser_manual_house_load`

If recent history is not representative, either change the averaging period or turn off calculated averaging and set the manual house-load fallback.

### The battery SOC looks wrong

Check the SOC entity unit:

- `%` is converted to kWh using configured battery capacity
- kWh values are used directly

If the entity is `unknown` or `unavailable`, the optimiser uses minimum SOC as a safe fallback.

### The automation does not control my inverter

This integration does not call inverter services directly. You need an automation that maps:

```text
charging / discharging / hold
```

to your inverter's own select, switch, number, or service calls.

---

## Development

Run tests:

```bash
python3 -m pytest tests/ -v
python3 -m compileall custom_components/battery_solar_optimiser
node --check www/bso-plan-card.js
```

---

## Current limitations

- No direct inverter control built in yet.
- No export/sell-back optimisation. This is deliberate for self-use/no-export-tariff setups.
- Load is averaged from recent history or a manual fallback, not a detailed time-of-day household profile.
- Solar forecast parsing supports common Forecast.Solar power/energy sensors, but may need tuning for different forecast integrations.
- Slot override entities are displayed as relative rows, but the selected override is pinned internally to the absolute slot start time.

---

## Roadmap ideas

- Optional direct inverter service mappings.
- Optional export-price / sell-back optimisation for users with a real export tariff.
- Time-varying load profiles beyond the current 24/48/72h average.
- Better solar forecast source adapters.
- A richer override editor for selecting calendar times directly rather than relative rows.
- Configurable debounce interval for interactive control changes (currently 30 seconds to avoid dropdowns closing while the user is still choosing).
- HACS-ready screenshots and richer documentation.

---

## License

MIT
