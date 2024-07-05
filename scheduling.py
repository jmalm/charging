"""App for scheduling charging."""
from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil

from dateutil import parser

from appdaemon.plugins.hass.hassapi import Hass

from charger import Charger


class Scheduler(Hass):
    charger = None
    smart_charge = False
    charge_now_switch = None
    state_of_charge_entity = 0
    departure_time = datetime.now()
    price_entity = None
    car_battery_size_kwh = 64
    target_state_of_charge = 100

    def initialize(self):
        # Charger and home
        charger_status_entity_id = str(self.args['charger_status_entity_id'])
        self.charger = Charger(self.get_entity(charger_status_entity_id), None, None)
        self.listen_state(self.charger_status_cb, charger_status_entity_id)

        # Shall we do smart charging?
        smart_charging_entity_id = str(self.args['smart_charging_entity_id'])
        self.smart_charge = self.get_state(smart_charging_entity_id) == 'on'
        self.listen_state(self.smart_charging_cb, smart_charging_entity_id)

        # The charge-now entity is the one we will use to control charging.
        charge_now_entity_id = str(self.args['charge_now_entity_id'])
        self.charge_now_switch = self.get_entity(charge_now_entity_id)

        # Current state of charge is used to decide how much the car needs to charge.
        state_of_charge_entity_id = str(self.args['state_of_charge_entity_id'])
        self.state_of_charge_entity = self.get_entity(state_of_charge_entity_id)
        self.listen_state(self.state_of_charge_cb, state_of_charge_entity_id)

        # When last known state of charge is updated by the user, we will reschedule.
        last_known_state_of_charge_entity_id = str(self.args['last_known_state_of_charge_entity_id'])
        self.listen_state(self.last_known_state_of_charge_cb, last_known_state_of_charge_entity_id)

        # When should charging be done?
        departure_time_entity_id = str(self.args['departure_time_entity_id'])
        departure_time_entity = self.get_entity(departure_time_entity_id)
        departure_time = self.parse_datetime(departure_time_entity.state, aware=True)
        self.set_departure_time(departure_time)
        self.listen_state(self.departure_time_cb, departure_time_entity_id)

        # Electricity price
        price_entity_id = str(self.args['price_entity_id'])
        self.price_entity = self.get_entity(price_entity_id)

        # Run scheduling every half hour + 1 minute.
        next_occurrence = ceil_dt(datetime.now(), timedelta(minutes=30)) + timedelta(minutes=1)
        # next_occurrence = datetime.now() + timedelta(minutes=1)
        self.log(f"Scheduling next at {next_occurrence}")
        self.run_every(self.scheduler_cb, next_occurrence, 30 * 60)

        self.schedule()

    def charger_status_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the charger status sensor."""
        self.schedule()

    def departure_time_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the departure time sensor."""
        self.set_departure_time(self.parse_datetime(new, aware=True))
        self.schedule()

    def smart_charging_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the smart charging switch."""
        self.smart_charge = new == 'on'
        self.log(f"Smart charging: {new}")
        self.schedule()

    def state_of_charge_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the state of charge sensor."""
        self.log(f"State of charge: {new} %")
        # TODO: Should we reschedule? Maybe if the state of charge has changed significantly?

    def scheduler_cb(self, *args, **kwargs):
        """Callback for the scheduler."""
        self.log(f"Scheduler callback called.")
        self.schedule()

    def last_known_state_of_charge_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the last known state of charge sensor."""
        self.log(f"Last known state of charge: {new} %")
        self.schedule()

    def set_departure_time(self, time: datetime):
        """Set the departure time."""
        departure_time = datetime(time.year, time.month, time.day, time.hour, time.minute, tzinfo=time.tzinfo)
        now = self.get_now()
        if departure_time > now:
            self.log(f"Departure time: {departure_time}")
        else:
            departure_time = datetime(now.year, now.month, now.day, 7, tzinfo=now.tzinfo)
            if now.hour > 7:
                departure_time = departure_time + timedelta(days=1)
            self.log(f"Departure time: {time} is in the past. Setting to 07:00.")
        self.departure_time = departure_time

    def schedule(self):
        """Schedule charging."""
        if not self.smart_charge:
            if self.charge_now_switch.get_state() == "off":
                self.log("Smart charging disabled, but charging is off. Enabling charging.")
                self.charge_now_switch.set_state(state="on",
                                                 attributes={"reason": "Smart charging disabled"})
            return

        # Assume that we will be running on 80 % of the full charging power.
        current_soc = float(self.state_of_charge_entity.state)
        if current_soc >= self.target_state_of_charge:
            self.log(f"Current state of charge ({current_soc}) is already above {self.target_state_of_charge}.")
            return
        num_hours_to_charge = self.get_min_hours_to_charge(current_soc, self.target_state_of_charge) / 0.8
        self.log(f"Number of hours to charge from {current_soc} to {self.target_state_of_charge} %: {num_hours_to_charge}")

        hourly_prices = self.get_prices()
        available_hours = [h for h in hourly_prices if self.in_time_slot(h['start']) or self.in_time_slot(h['end'])]
        sorted_hourly_prices = sorted(available_hours, key=lambda x: x['value'])
        hours_to_charge = sorted_hourly_prices[:ceil(num_hours_to_charge)]
        contiguous_slots = self.get_contiguous_slots([{'start': h['start'], 'end': h['end']} for h in hours_to_charge])
        self.log(f"Charging plan:\n{contiguous_slots}")
        estimated_cost = sum([h['value'] for h in hours_to_charge])
        currency = str(self.price_entity.attributes.get("currency"))
        self.log(f"Estimated cost: {estimated_cost:.2f} {currency}")

        # Charge when in time slot.
        if self.in_time_slot(self.get_now(), start=contiguous_slots[0]['start'], end=contiguous_slots[0]['end']):
            target_state = "on"
            if self.charge_now_switch.state == "off":
                self.log("Enabling charging because of schedule.")
            else:
                self.log("Charging is already enabled.", level="DEBUG")
        else:
            target_state = "off"
            if self.charge_now_switch.state == "on":
                self.log("Disabling charging because of schedule.")
            else:
                self.log("Charging is already disabled.", level="DEBUG")
        # Set state, including schedule attribute (which may have changed even if charge-now didn't).
        self.charge_now_switch.set_state(state=target_state, attributes={"reason": f"scheduled {target_state}",
                                                                         "schedule": contiguous_slots})

    def get_min_hours_to_charge(self, current_soc, target_soc=100):
        """Get the minimum number of hours that the car needs to be charged, i.e. at max charging power."""
        current_kwh = current_soc / 100 * self.car_battery_size_kwh
        target_kwh = self.car_battery_size_kwh * target_soc / 100
        kwh_to_charge = target_kwh - current_kwh
        max_power_kw = self.charger.max_charging_current * 230 / 1000
        hours_to_charge = kwh_to_charge / max_power_kw
        return hours_to_charge

    def get_prices(self):
        tomorrow = self.price_entity.attributes.get("raw_tomorrow", [])
        today = self.price_entity.attributes.get("raw_today", [])
        # currency = str(self.price_entity.attributes.get("currency"))
        hourly_prices = []
        for i in today + tomorrow:
            hourly_prices.append({
                'start': parser.parse(i['start']),
                'end': parser.parse(i['end']),
                'value': i['value']})
        return hourly_prices

    def in_time_slot(self, time: datetime, start: datetime = None, end: datetime = None):
        if start is None:
            start = self.get_now()
        if end is None:
            end = self.departure_time
        return start < time < end

    def get_contiguous_slots(self, slots: list[dict[str, datetime]]) -> list[dict[str, datetime]]:
        """Get the contiguous slots of the given prices."""
        sorted_slots = sorted(slots, key=lambda x: x['start'])
        contiguous_slots = []
        for slot in sorted_slots:
            if len(contiguous_slots) == 0:
                contiguous_slots.append(slot)
            elif contiguous_slots[-1]['end'] == slot['start']:
                contiguous_slots[-1]['end'] = slot['end']
            else:
                contiguous_slots.append(slot)
        return contiguous_slots


def ceil_dt(dt, delta):
    import math
    return datetime.min + math.ceil((dt - datetime.min) / delta) * delta
