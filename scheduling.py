"""App for scheduling charging."""
from __future__ import annotations

from datetime import datetime, timedelta
from dateutil import parser
from typing import Any

from appdaemon.plugins.hass.hassapi import Hass

from charger import Charger


# The electrical grid voltage
VOLTAGE = 230


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
        #       Why do we even have both state_of_charge_entity and last_known_state_of_charge_entity?
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
        current_soc = float(self.state_of_charge_entity.state)
        time_to_charge = self.estimate_time_to_charge(current_soc, self.target_state_of_charge)

        if not self.smart_charge:
            await self.not_smart_charging(time_to_charge)
            return

        # TODO: If not connected, we should not charge.

        if current_soc >= self.target_state_of_charge:
            await self.target_reached(current_soc)

        self.log(f"Estimated time to charge from {current_soc} to {self.target_state_of_charge} %: {time_to_charge}")

        available_periods = self.get_prices(await self.get_now(), self.departure_time)
        try:
            charging_slots = create_schedule(available_periods, time_to_charge)
        except NotEnoughTimeException:
            await self.not_enough_time(time_to_charge)
            return

        # Charge when in time slot.
        await self.charge_in_time_slot(charging_slots, time_to_charge)

    async def target_reached(self, current_soc):
        if self.target_state_of_charge >= 100:
            # The target state of charge is 100 %. Just leave the charging on.
            self.log(f"Target state of charge is 100 %. Leaving charging on.")
            return
        self.log(f"Current state of charge ({current_soc}) is above {self.target_state_of_charge}.")
        if self.charge_now_switch.state == "on":
            self.log("Charging is on. Disabling charging.")
            reason = f"Target state of charge {self.target_state_of_charge} reached"
            await self.set_charge_now_switch(state="off", reason=reason)

    async def not_smart_charging(self, time_to_charge: timedelta):
        eta = await self.get_now() + time_to_charge if time_to_charge else None
        charging = self.charge_now_switch.get_state() == "on"
        if charging and eta:
            self.log(f"Updating ETA ({eta})")
        elif not charging:
            self.log("Smart charging disabled, but charging is off. Enabling charging.")
        else:
            return
        await self.set_charge_now_switch(state="on",
                                         reason="Smart charging disabled",
                                         eta=eta)

    async def not_enough_time(self, needed_time: timedelta):
        """Starts charging when there is not enough time to charge to the desired state of charge."""
        eta = calculate_eta(await self.get_now(), needed_time)
        if self.charge_now_switch.state == "off":
            self.log(f"Not enough time to charge to {self.target_state_of_charge} %, but charging is off. "
                     f"Enabling charging. ETA: {eta}")
        # Always set the state, including reason.
        self.log(f"Not enough time to charge to {self.target_state_of_charge} %. ETA: {eta}")
        await self.set_charge_now_switch(state="on",
                                         reason="Not enough time to charge",
                                         eta=eta)

    async def charge_in_time_slot(self, contiguous_slots: list[dict], needed_time: timedelta):
        """Starts charging when in a scheduled charging time slot."""
        now = await self.get_now()
        if in_time_slot(now, start=contiguous_slots[0]['start'], end=contiguous_slots[0]['end']):
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
        eta = calculate_eta(now, needed_time, contiguous_slots)
        # Set state, including reason, eta, and schedule attributes (which may have changed even if charge-now didn't).
        await self.set_charge_now_switch(state=target_state,
                                         reason=f"scheduled {target_state}",
                                         eta=eta,
                                         schedule=contiguous_slots)

    async def set_charge_now_switch(self,
                                    state: str,
                                    reason: str,
                                    eta: datetime | None = None,
                                    schedule: list[dict] | None = None):
        attributes = {"reason": reason}
        if eta:
            attributes['eta'] = str(eta)
        if schedule:
            attributes['schedule'] = schedule
        self.log(f"Setting charge now switch {state} {attributes}")

        await self.charge_now_switch.set_state(state=state, attributes=attributes, replace=True)

    def estimate_time_to_charge(self, current_soc, target_soc=100):
        if current_soc >= target_soc:
            return timedelta(0)
        energy_to_charge_kwh = (self.target_state_of_charge - current_soc) / 100 * self.car_battery_size_kwh
        min_charge_time = charge_time(energy_to_charge_kwh, self.charger.max_charging_current)
        return min_charge_time / 0.8  # Assume averaging charging rate at 80 % of max.

    def get_prices(self, start: datetime, end: datetime):
        tomorrow = self.price_entity.attributes.get("raw_tomorrow", [])
        today = self.price_entity.attributes.get("raw_today", [])
        known_prices = parse_prices(today + tomorrow)
        try:
            return get_prices(known_prices, start, end)
        except IndexError:
            # I have once seen this happen, but wasn't able to find the cause. Log input data in case it happens again.
            self.error(f"Failed to get prices (known prices: {known_prices}, start: {start}, end: {end}")
            raise


def create_schedule(available_periods: list[dict[str, datetime]], needed_time: timedelta) -> list[dict[str, datetime]]:
    if len(available_periods) == 0:
        raise NotEnoughTimeException(needed_time, timedelta(hours=0))
    periods_by_price = sorted(available_periods, key=lambda x: x['value'])

    periods_to_charge = []
    used_time = timedelta(0)
    for period in periods_by_price:
        periods_to_charge.append(period)
        used_time += period['end'] - period['start']
        if used_time >= needed_time:
            break
    else:
        raise NotEnoughTimeException(needed_time, used_time)

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


def get_prices(known_prices: list[dict], start: datetime, end: datetime) -> list[dict]:
    if start < known_prices[0]['start']:
        raise ValueError(f"Start time {start} is before the first known price {known_prices[0]['start']}. This is not supported.")
    prices = extrapolate_prices(known_prices, end)

    prices = [h for h in prices if
              (start <= h['start'] < end) or
              (start < h['end'] <= end)]

    # Start the first slot at the start time. End the last slot at the end time.
    assert prices[0]['start'] <= start < prices[0]['end'], f"Start time {start} should be within the first price slot {prices[0]}."
    assert prices[-1]['start'] < end <= prices[-1]['end'], f"End time {end} should be within the last price slot {prices[-1]}."
    prices[0]['start'] = start
    prices[-1]['end'] = end

    return prices


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
    filled_end = lambda : filled[-1]['end']

    # Find the corresponding period the day before.
    # Assume that all periods have the same duration.
    get_previous_day_period = lambda start: next((p for p in filled if p['start'] >= start - timedelta(days=1)))

    # Fill missing periods.
    while filled_end() < end:
        previous_day_period = get_previous_day_period(filled_end())
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


def calculate_eta(now: datetime, expected_charge_time: timedelta, schedule: list[dict] = None) -> datetime:
    """Calculates the estimated time when charging is done."""
    start = now
    charge_time_left = expected_charge_time
    for slot in (schedule or []):
        if slot['end'] <= start:
            # Don't use this slot
            continue
        start = start if start > slot['start'] else slot['start']

        if start + charge_time_left < slot['end']:
            # Charging is completed during the slot.
            return start + charge_time_left

        # Use the whole slot for charging.
        charge_time_left -= slot['end'] - start
        start = slot['end']
        continue

    # No slots left. Charging will continue until it is completed.
    return start + charge_time_left


def charge_time(energy_kwh: float, current_a: float) -> timedelta:
    max_power_kw = current_a * VOLTAGE / 1000
    hours_to_charge = energy_kwh / max_power_kw
    return timedelta(hours=hours_to_charge)
