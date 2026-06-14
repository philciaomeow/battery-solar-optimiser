# Battery Solar Optimiser

A lightweight Home Assistant custom component for battery + solar + Agile import-price optimisation. Designed as a simpler, more stable alternative for users who don't need a complex battery controller.

## What it does

- Reads your Octopus Agile import prices.
- Reads a solar forecast.
- Reads your current battery SOC.
- Predicts your home load (fixed W per hour).
- Plans **charging / discharging / hold** slots over the next 24 hours.
- Exposes the current recommended action as a `select` entity you can use in automations.

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.battery_solar_optimiser_plan` | Sensor | Current slot action plus full 48-slot plan attributes |
| `sensor.battery_solar_optimiser_estimated_cost` | Sensor | Estimated import cost over the plan horizon |
| `sensor.battery_solar_optimiser_next_action` | Sensor | Next non-hold action and time |
| `select.battery_solar_optimiser_action` | Select | Recommended action: `charging`, `discharging`, `hold` |

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant.
2. Add this repository as a custom repository (`https://github.com/phil-ciaomeow/battery-solar-optimiser`).
3. Install **Battery Solar Optimiser**.
4. Restart Home Assistant.
5. Go to **Settings > Devices & Services > Add Integration > Battery Solar Optimiser**.

### Manual

1. Copy `custom_components/battery_solar_optimiser` into `config/custom_components/`.
2. Restart Home Assistant.
3. Add the integration via the UI.

## Configuration

- **Agile price entity** — Octopus Agile sensor
- **Solar forecast entity** — e.g. Solcast
- **Battery SOC entity** — kWh remaining
- **Inverter mode entity** — reserved for future control expansion
- **Battery capacity** — kWh
- **Minimum SOC** — kWh; when SOC drops below this the plan refreshes every 5 minutes
- **Max charge / discharge power** — kW
- **Round-trip efficiency** — 0.0–1.0
- **Hourly load** — average W

## Scheduling logic

- The plan refreshes at **:25** and **:55** each hour, i.e. 5 minutes before each Agile slot boundary.
- If battery SOC is below **Minimum SOC**, it refreshes every 5 minutes until it recovers.

## How the optimisation works

1. For each 30-minute slot it estimates solar generation and home consumption.
2. Solar first covers load, then charges the battery.
3. Any remaining load is discharged from the battery (never below minimum SOC).
4. Finally, cheap Agile slots trigger extra grid charging if a future price rise makes it worthwhile.

## Automations

Use the `select.battery_solar_optimiser_action` entity to drive your inverter:

```yaml
alias: "Battery optimiser control"
trigger:
  - platform: state
    entity_id: select.battery_solar_optimiser_action
action:
  - choose:
      - conditions:
          - condition: state
            entity_id: select.battery_solar_optimiser_action
            state: "charging"
        sequence:
          - service: number.set_value
            target:
              entity_id: number.my_inverter_charge_rate
            data:
              value: 3500
      - conditions:
          - condition: state
            entity_id: select.battery_solar_optimiser_action
            state: "discharging"
        sequence:
          - service: number.set_value
            target:
              entity_id: number.my_inverter_discharge_rate
            data:
              value: 3500
      - conditions:
          - condition: state
            entity_id: select.battery_solar_optimiser_action
            state: "hold"
        sequence:
          - service: number.set_value
            target:
              entity_id: number.my_inverter_charge_rate
            data:
              value: 0
```

## Roadmap

- Export/sell-back optimisation
- Time-varying load profile
- Direct inverter mode service calls
- Solar forecast source alternatives

## License

MIT
