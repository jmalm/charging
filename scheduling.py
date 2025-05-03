"""App for scheduling charging."""
from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil
from typing import Any

from dateutil import parser

from appdaemon.plugins.hass.hassapi import Hass

from charger import Charger


class Scheduler(Hass):
    charger: Charger = None
    smart_charge = False
    charge_now_switch = None
    state_of_charge_entity = 0
    departure_time = datetime.now()
    price_entity = None
    car_battery_size_kwh = 64
    target_state_of_charge = 100
    reschedule_on_next_state_of_charge_change = False

    async def initialize(self):
        # Charger and home
        charger_status_entity_id = str(self.args['charger_status_entity_id'])
        self.charger = Charger(self.get_entity(charger_status_entity_id), None, None)
        await self.listen_state(self.charger_status_cb, charger_status_entity_id)

        # Shall we do smart charging?
        smart_charging_entity_id = str(self.args['smart_charging_entity_id'])
        self.smart_charge = await self.get_state(smart_charging_entity_id) == 'on'
        await self.listen_state(self.smart_charging_cb, smart_charging_entity_id)

        # The charge-now entity is the one we will use to control charging.
        charge_now_entity_id = str(self.args['charge_now_entity_id'])
        self.charge_now_switch = self.get_entity(charge_now_entity_id)

        # The current state of charge is used to decide how much the car needs to charge.
        state_of_charge_entity_id = str(self.args['state_of_charge_entity_id'])
        self.state_of_charge_entity = self.get_entity(state_of_charge_entity_id)
        await self.listen_state(self.state_of_charge_cb, state_of_charge_entity_id)

        # When the last known state of charge is updated by the user, we will reschedule.
        last_known_state_of_charge_entity_id = str(self.args['last_known_state_of_charge_entity_id'])
        await self.listen_state(self.last_known_state_of_charge_cb, last_known_state_of_charge_entity_id)

        # When should charging be done?
        departure_time_entity_id = str(self.args['departure_time_entity_id'])
        departure_time_entity = self.get_entity(departure_time_entity_id)
        departure_time = await self.parse_datetime(departure_time_entity.state, aware=True)
        await self.set_departure_time(departure_time)
        await self.listen_state(self.departure_time_cb, departure_time_entity_id)

        # Electricity price
        price_entity_id = str(self.args['price_entity_id'])
        self.price_entity = self.get_entity(price_entity_id)

        # Run scheduling every half hour + 1 minute.
        next_occurrence = round_datetime_up(await self.get_now(), timedelta(minutes=30), timedelta(minutes=1))
        # next_occurrence = datetime.now() + timedelta(minutes=1)
        self.log(f"Scheduling next at {next_occurrence}")
        await self.run_every(self.scheduler_cb, next_occurrence, 30 * 60)

        await self.handle_current_state()

    async def charger_status_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the charger status sensor."""
        await self.handle_current_state()

    async def departure_time_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the departure time sensor."""
        await self.set_departure_time(await self.parse_datetime(new, aware=True))
        await self.handle_current_state()

    async def smart_charging_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the smart charging switch."""
        self.smart_charge = new == 'on'
        self.log(f"Smart charging: {new}")
        await self.handle_current_state()

    async def state_of_charge_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the state of charge sensor."""
        self.log(f"State of charge: {new} %")
        # TODO: Should we reschedule? Maybe if the state of charge has changed significantly?
        if self.reschedule_on_next_state_of_charge_change:
            self.reschedule_on_next_state_of_charge_change = False
            await self.handle_current_state()

    async def scheduler_cb(self, *args, **kwargs):
        """Callback for the scheduler."""
        self.log(f"Scheduler callback called.")
        await self.handle_current_state()

    async def last_known_state_of_charge_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the last known state of charge sensor."""
        self.log(f"Last known state of charge: {new} %")
        # The state_of_charge_entity may not yet have been updated, if it is a calculated entity, based on
        # last_known_state_of_charge_entity.
        self.reschedule_on_next_state_of_charge_change = True
        await self.handle_current_state()

    async def set_departure_time(self, time: datetime):
        """Set the departure time."""
        departure_time = datetime(time.year, time.month, time.day, time.hour, time.minute, tzinfo=time.tzinfo)
        now = await self.get_now()
        if departure_time > now:
            self.log(f"Departure time: {departure_time}")
        else:
            departure_time = datetime(now.year, now.month, now.day, 7, tzinfo=now.tzinfo)
            if now > departure_time:
                departure_time = departure_time + timedelta(days=1)
            self.log(f"Departure time: {time} is in the past. Setting to 07:00.")
        self.departure_time = departure_time

    async def handle_current_state(self):
        """Schedule charging."""
        if not self.smart_charge:
            await self.not_smart_charging()
            return

        # TODO: If not connected, we should not charge.

        current_soc = float(self.state_of_charge_entity.state)
        if current_soc >= self.target_state_of_charge:
            await self.target_reached(current_soc)

        # Assume that we will be running on 80 % of the full charging power.
        num_hours_to_charge = self.get_min_hours_to_charge(current_soc, self.target_state_of_charge) / 0.8
        self.log(f"Number of hours to charge from {current_soc} to {self.target_state_of_charge} %: {num_hours_to_charge}")

        available_periods = self.get_prices(await self.get_now(), self.departure_time)
        try:
            charging_slots = create_schedule(available_periods, num_hours_to_charge)
        except NotEnoughTimeException:
            await self.not_enough_time(num_hours_to_charge)
            return
        self.log(f"Charging plan:\n{charging_slots}")

        # Charge when in time slot.
        await self.charge_in_time_slot(charging_slots)

    async def target_reached(self, current_soc):
        if self.target_state_of_charge >= 100:
            # The target state of charge is 100 %. Just leave the charging on.
            self.log(f"Target state of charge is 100 %. Leaving charging on.")
            return
        self.log(f"Current state of charge ({current_soc}) is above {self.target_state_of_charge}.")
        if self.charge_now_switch.state == "on":
            self.log("Charging is on. Disabling charging.")
            reason = f"Target state of charge {self.target_state_of_charge} reached"
            await self.charge_now_switch.set_state(state="off", attributes={"reason": reason}, replace=True)

    async def not_smart_charging(self):
        if self.charge_now_switch.get_state() == "off":
            self.log("Smart charging disabled, but charging is off. Enabling charging.")
            await self.charge_now_switch.set_state(state="on",
                                                   attributes={"reason": "Smart charging disabled"})

    async def not_enough_time(self, num_hours_to_charge):
        """Starts charging when there is not enough time to charge to the desired state of charge."""
        eta = await self.get_now() + timedelta(hours=num_hours_to_charge)
        if self.charge_now_switch.state == "off":
            self.log(f"Not enough time to charge to {self.target_state_of_charge} %, but charging is off. "
                     f"Enabling charging. ETA: {eta}")
        # Always set the state, including reason.
        self.log(f"Not enough time to charge to {self.target_state_of_charge} %. ETA: {eta}")
        await self.charge_now_switch.set_state(state="on", attributes={"reason": "Not enough time to charge", "eta": eta},
                                               replace=True)

    async def charge_in_time_slot(self, contiguous_slots):
        """Starts charging when in a scheduled charging time slot."""
        if in_time_slot(await self.get_now(), start=contiguous_slots[0]['start'], end=contiguous_slots[0]['end']):
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
        # Set state, including reason and schedule attributes (which may have changed even if charge-now didn't).
        await self.charge_now_switch.set_state(state=target_state, attributes={"reason": f"scheduled {target_state}",
                                                                               "schedule": contiguous_slots})

    def get_min_hours_to_charge(self, current_soc, target_soc=100):
        """Get the minimum number of hours that the car needs to be charged, i.e. at max charging power."""
        current_kwh = current_soc / 100 * self.car_battery_size_kwh
        target_kwh = self.car_battery_size_kwh * target_soc / 100
        kwh_to_charge = target_kwh - current_kwh
        max_power_kw = self.charger.max_charging_current * 230 / 1000
        hours_to_charge = kwh_to_charge / max_power_kw
        return hours_to_charge

    def get_prices(self, start: datetime, end: datetime):
        tomorrow = self.price_entity.attributes.get("raw_tomorrow", [])
        today = self.price_entity.attributes.get("raw_today", [])
        return get_prices(parse_prices(today + tomorrow), start, end)


def create_schedule(available_periods: list[dict[str, datetime]], num_hours_to_charge: float) -> list[dict[str, datetime]]:
    needed_time = num_hours_to_charge * timedelta(hours=1)
    if len(available_periods) == 0:
        raise NotEnoughTimeException(needed_time, timedelta(hours=0))
    periods_by_price = sorted(available_periods, key=lambda x: x['value'])
    period = available_periods[0]['end'] - available_periods[0]['start']
    available_time = period * len(available_periods)
    num_periods_to_charge = ceil(num_hours_to_charge * timedelta(hours=1) / period)
    if num_periods_to_charge > len(periods_by_price):
        raise NotEnoughTimeException(needed_time, available_time)
    periods_to_charge = periods_by_price[:num_periods_to_charge]
    contiguous_slots = get_contiguous_slots([{'start': h['start'], 'end': h['end']} for h in periods_to_charge])

    # TODO: The following is completely wrong.
    #       1. We have to multiply with the expected power (80 % of full charging power, according to how we
    #       calculate the number of hours to charge).
    #       2. The first and last hours will not be full hours.
    # estimated_cost = sum([h['value'] for h in hours_to_charge])
    # currency = str(self.price_entity.attributes.get("currency"))
    # self.log(f"Estimated cost: {estimated_cost:.2f} {currency}")

    return contiguous_slots


class NotEnoughTimeException(Exception):
    def __init__(self, needed_time: timedelta, available_time: timedelta):
        self.needed_time = needed_time
        self.available_time = available_time


def parse_prices(prices: list[dict]) -> list[dict]:
    return [{
            'start': parser.parse(p['start']),
            'end': parser.parse(p['end']),
            'value': float(p['value'])
        } for p in prices]


def get_prices(known_prices, start, end):
    prices = extrapolate_prices(known_prices, end)
    return [h for h in prices if
            (start <= h['start'] < end) or
            (start < h['end'] <= end)]


def in_time_slot(time: datetime, start: datetime, end: datetime):
    return start <= time < end


def get_contiguous_slots(slots: list[dict[str, datetime]]) -> list[dict[str, datetime]]:
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


def round_datetime_up(
        ts: datetime,
        delta: timedelta,
        offset: timedelta = timedelta(minutes=0)) -> datetime:
    """Snap to next available timedelta.

    Preserve any timezone info on `ts`.

    If we are at the given exact delta, then do not round, only add offset.

    :param ts: Timestamp we want to round
    :param delta: Our snap grid
    :param offset: Add a fixed time offset at the top of rounding
    :return: Rounded up datetime

    From https://stackoverflow.com/a/71482147/442138.
    """
    rounded = ts + (datetime.min.replace(tzinfo=ts.tzinfo) - ts) % delta
    return rounded + offset


def extrapolate_prices(prices: list[dict[str, Any]], end: datetime) -> list[dict[str, Any]]:
    """
    Fill missing periods at the end of *prices*, assuming that prices
    will be the same as the same period the preceding day.
    """
    filled = [p for p in prices]
    existing_end = filled[-1]['end']

    # Find the corresponding period the day before.
    # Assume that all periods have the same duration.
    previous_day_periods = (f for f in filled if f['start'] >= existing_end - timedelta(days=1))

    # Fill missing periods.
    while filled[-1]['end'] < end:
        previous_day_period = next(previous_day_periods)
        period = {
            'start': previous_day_period['start'] + timedelta(days=1),
            'end': previous_day_period['end'] + timedelta(days=1),
            'value': previous_day_period['value']
        }
        filled.append(period)

    # Make sure the last period ends at the requested end time.
    if filled[-1]['start'] < end < filled[-1]['end']:
        filled[-1]['end'] = end

    return filled
