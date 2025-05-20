# Charging

Charge your electric vehicle during the least expensive hours of the day, with load balancing to avoid overloading your
circuit.

It is a [Home Assistant](https://www.home-assistant.io/) [AppDaemon](https://appdaemon.readthedocs.io/) app. It uses entities in home assistant to read the current state of charge and current
power usage.

## Features

- Set departure time
- Set target charge level
- Load balance to avoid overloading your circuit
- Enable/disable smart charging via switch in home assistant
- When smart charging is disabled, switch charging on/off via switch in home assistant
- Charger dynamic circuit limit set to 10 A when disconnected from charger

## Limitations

- Currently only supports Easee chargers (Easee Home tested)
- Currently only supports 1-phase charging (should be easy to add support for 3-phase, but I don't have a vehicle that supports it)

## Usage

### Setup

1. Install [AppDaemon](https://appdaemon.readthedocs.io/) in your home assistant instance
2. Add this repository to your appdaemon configuration, in the `apps` directory
3. In home assistant, create the following helpers:
   - `Car smart charging` (input boolean)
   - `Anticipated departure time` (input datetime)
   - `Car charge now` (input boolean)
   - `Desired state of charge at departure` (input number)
   - `Car load balance` (input boolean)
   - `Car one phase charging` (input boolean)
4. In `apps.yaml`, add the following, adjusted to your setup:

```yaml
scheduling:
  module: scheduling
  class: Scheduler
  charger_status_entity_id: sensor.easee_home_xxxxx_status
  smart_charging_entity_id: input_boolean.car_smart_charging
  departure_time_entity_id: input_datetime.anticipated_departure_time
  charge_now_entity_id: input_boolean.car_charge_now
  desired_state_of_charge_at_departure_entity_id: input_number.desired_state_of_charge_at_departure
  state_of_charge_entity_id: input_number.estimated_state_of_charge
  last_known_state_of_charge_entity_id: sensor.wican_soc_d
  price_entity_id: sensor.nordpool_kwh_se3_sek_3_10_025
  car_battery_size_kwh: 64

load_balancing:
  module: load_balancing
  class: LoadBalancer
  charger_status_entity: sensor.easee_home_xxxxx_status
  load_balancing_entity_id: input_boolean.car_load_balance
  smart_charging_entity_id: input_boolean.car_smart_charging
  one_phase_charging_entity_id: input_boolean.car_one_phase_charging
  charge_now_entity_id: input_boolean.car_charge_now
  current_l1_entity_id: sensor.lowpass_current_l1
  current_l2_entity_id: sensor.lowpass_current_l2
  current_l3_entity_id: sensor.lowpass_current_l3
  charger_status_entity_id: sensor.easee_home_xxxxx_status
  charger_current_entity_id: sensor.easee_home_xxxxx_current
  circuit_dynamic_limit_entity_id: sensor.easee_home_xxxxx_dynamic_circuit_limit

```

The schedule and estimated time of reaching the desired state of charge are added as attributes to the `Car charge now`
entity. This can be used to visualize the charging schedule in, for example, a plotly-graph card:

```yaml
type: custom:plotly-graph
time_offset: 1.1d
refresh_interval: 10
hours_to_show: 48
layout:
  xaxis:
    rangeselector:
      "y": 1.4
  yaxis:
    rangemode: tozero
    tickformat: .1f
  yaxis2:
    rangemode: tozero
    tickformat: .2f
  yaxis9:
    visible: false
    fixedrange: true
round: 2
default: null
entities:
  - entity: sensor.nordpool_kwh_se3_sek_3_10_025
    yaxis: "y"
    name: Nordpool price
    fill: tozeroy
    fillcolor: rgba(255, 165, 0,.1)
    line:
      color: rgba(255, 165, 0, 0.7)
      width: 1
    filters:
      - fn: |-
          ({ meta }) => ({
            xs: meta.raw_today.concat(...meta.raw_tomorrow).map(({ start }) => new Date(start)),
            ys: meta.raw_today.concat(...meta.raw_tomorrow).map(({ value }) => value),
          })
      - exponential_moving_average:
          alpha: 1
  - entity: sensor.easee_home_xxxxx_current
    line:
      color: rgba(128,0,0,0.7)
  - entity: sensor.easee_home_xxxxx_dynamic_circuit_limit
    line:
      color: rgba(128,128,128,0.3)
  - entity: ""
    name: Now
    yaxis: y9
    showlegend: false
    line:
      width: 1
      dash: dot
      color: deepskyblue
    x: $fn () => [Date.now(), Date.now()]
    "y": $fn () => [0,1]
  - entity: input_boolean.car_charge_now
    name: Charging plan
    fill: tozeroy
    line:
      color: lightblue
      width: 1
    filters:
      - fn: |-
          ({meta}) => {
            let schedule = meta.schedule;
            if (schedule === undefined) {
              return {
                xs: [],
                ys: []
              };
            }
            const xs = [];
            const ys = [];
            for (let i = 0; i < schedule.length; i++) {
              xs.push(schedule[i].start);
              ys.push(1);
              xs.push(schedule[i].end);
              ys.push(0);
            }
            return { xs: xs, ys: ys };
          }
```

## Usage

In home assistant, switch on the following:
- `Car smart charging` (input boolean)
- `Car load balance` (input boolean)
- `Car one phase charging` (input boolean)

When you connect the charger, set the departure time and desired state of charge at departure. The app will calculate
a charging schedule, optimized for cost, and enable charging during the least expensive hours.

Charging is regulated by setting the circuit dynamic limit to how much is available on the circuit, based on other load.
If ever the circuit dynamic limit needs to be manually reset (for example if you've disabled / uninstalled this app),
this can be done in the Easee app.

If the departure time is beyond the time for which the price is known, the app will repeat the last day's prices.

## Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Contact

Jakob Malm - [@jmalm](https://github.com/jmalm)

Project Link: [https://github.com/jmalm/charging](https://github.com/jmalm/charging)

