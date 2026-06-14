# Battery Solar Optimiser

Battery Solar Optimiser is a lightweight Home Assistant custom integration for planning simple battery charge, discharge, and hold windows from:

- Octopus Agile import prices
- optional previous-day Agile prices for unpublished future slots
- solar forecast / PV estimate
- current battery state of charge
- configurable household load
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
- Uses actual Octopus Agile rates when available.
- Uses previous-day same-slot Agile prices when future Agile slots are unpublished.
- Falls back to a configurable pessimistic missing-rate price when no tariff data exists.
- Includes recent historical price context so flat-looking prices after cheap periods are not treated as automatically cheap.
- Handles negative Agile prices and will recharge during cheap/negative post-peak periods when there is battery capacity available.
- Reads battery SOC as either `%` or `kWh`.
- Protects a configurable minimum SOC.
- Refreshes shortly before Agile slot changes.
- Exposes a manual recalculate button.
- Exposes manual per-slot override controls.
- Provides a ready-made Lovelace dashboard with inline override dropdowns.

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
| `select.battery_solar_optimiser_action` | Select | Current effective action: `charging`, `discharging`, or `hold`. Use this in inverter automations. |
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

The slot numbers are relative to the current 24-hour plan. Slot `00` is the first half-hour slot in the current plan, slot `47` is the last.

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
| Previous-day rates entity | Optional but recommended. Used for unpublished future Agile slots. If left blank, the integration tries to infer it from the current-day entity name. |
| Solar forecast entity | Forecast.Solar / Solcast-style entity with forecast attributes, or a Forecast.Solar power/energy sensor such as `sensor.power_production_now`. W/kW power sensors are converted to kWh per 30-minute slot. If unavailable, the integration uses a simple PV-capacity fallback estimate. |
| Battery SOC entity | Battery state of charge. Can be `%` or `kWh`. If unavailable, the optimiser falls back safely to minimum SOC. |
| Battery capacity | Usable battery capacity in kWh. |
| Minimum SOC | Reserve in kWh. The optimiser will not intentionally discharge below this. |
| PV capacity | Installed PV size / fallback solar estimate basis. |
| Max charge power | Maximum grid/battery charge power in kW. |
| Max discharge power | Maximum battery discharge power in kW. |
| Round-trip efficiency | Battery efficiency, usually around `0.90`–`0.95`. |
| Hourly load | Approximate average home load in watts. |
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

It shows:

- a full-width heading
- projected battery graph
- Agile price and solar forecast graph
- a wide 24-hour plan table
- inline per-slot override dropdowns
- status and freshness information
- totals
- optimiser history

The dashboard uses two small local custom cards from this repository:

```text
www/bso-plan-card.js
www/bso-layout-card.js
```

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

3. Register the custom card resources in `configuration.yaml`.

   If your Home Assistant uses YAML-mode Lovelace, add them under the global `lovelace.resources` section:

   ```yaml
   lovelace:
     resource_mode: yaml
     resources:
       - url: /local/bso-plan-card.js
         type: module
       - url: /local/bso-layout-card.js
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
6. If the custom card still shows a red configuration error, hard-refresh your browser. Custom card resources are often cached.

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
9. Apply any manual slot overrides.
10. Simulate projected SOC and cost from the resulting actions.

It is not a mathematical optimiser and does not use an LP/MILP solver. That is deliberate: the aim is a transparent, dependency-light controller that is easy to understand and debug.

---

## Troubleshooting

### The dashboard shows a red custom card configuration error

Check that both JS files are available:

```text
/local/bso-plan-card.js
/local/bso-layout-card.js
```

Then check they are registered under global Lovelace resources in `configuration.yaml`, not only inside an individual dashboard YAML file.

Restart Home Assistant and hard-refresh your browser.

### The plan has fallback prices

The plan attributes include price source counts. If many slots use fallback, check your Octopus current-day and previous-day rates entities.

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
node --check www/bso-layout-card.js
```

---

## Current limitations

- No direct inverter control built in yet.
- No export/sell-back optimisation yet.
- Load is a fixed average value, not a learned household profile.
- Solar forecast parsing is intentionally basic and may need tuning for different forecast integrations.
- Slot overrides are relative to the current rolling 24-hour plan, not pinned to absolute calendar times.

---

## Roadmap ideas

- Optional direct inverter service mappings.
- Export-price / sell-back optimisation.
- Time-varying load profiles.
- Better solar forecast source adapters.
- Absolute-time manual overrides.
- HACS-ready screenshots and richer documentation.

---

## License

MIT
