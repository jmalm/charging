import appdaemon.plugins.hass.hassapi as hass
from appdaemon.entity import Entity
from datetime import datetime
from dateutil import parser, tz


class StateOfChargeCalculator(hass.Hass):
    def initialize(self):
        self.battery_size_kWh = int(self.args['battery_size_kWh'])
        self.charger_energy_entity_id = str(self.args['charger_energy_entity_id'])
        self.last_known_state_of_charge_entity_id = str(self.args['last_known_state_of_charge_entity_id'])
        self.estimated_state_of_charge_entity_id = str(self.args['estimated_state_of_charge_entity_id'])
        self.car_soc_d_entity_id = str(self.args['car_soc_d_entity_id'])

        self.charger_energy_entity = self.get_entity(self.charger_energy_entity_id)
        self.last_known_state_of_charge_entity = self.get_entity(self.last_known_state_of_charge_entity_id)
        self.estimated_state_of_charge_entity = self.get_entity(self.estimated_state_of_charge_entity_id)
        self.car_soc_d_entity = self.get_entity(self.car_soc_d_entity_id)

        # Register callbacks
        self.listen_state(self.estimate, self.charger_energy_entity_id)
        self.listen_state(self.estimate, self.last_known_state_of_charge_entity_id)
        self.listen_state(self.update_last_known_state_of_charge, self.car_soc_d_entity_id)

        # Do the calculation (mainly for development purposes)
        self.estimate(None, None, None, None, None)


    def estimate(self, entity, attribute, old, new, kwargs):
        # TODO: What if the car has been disconnected since last known state?
        # Should we reset the last known state?
        # Should we adjust estimation depending on how much time has passed since the car was disconnected?
        # Should we clear the state (if that's even possible)?
        estimated_soc = self.estimate_state_of_charge(float(self.last_known_state_of_charge_entity.state),
                                                 parser.parse(self.last_known_state_of_charge_entity.last_changed),
                                                 float(self.battery_size_kWh),
                                                 self.charger_energy_entity)
        self.estimated_state_of_charge_entity.set_state(state=round(estimated_soc))

    def estimate_state_of_charge(self, known_state_of_charge: float, last_updated: datetime,
                                 battery_size_kwh: float, charger_energy_entity: Entity) -> float:
        """Estimate the state of charge right now, based on last known state of charge and the charger energy
         consumption.
         """
        state_of_charge_kwh = known_state_of_charge / 100 * battery_size_kwh

        # Add charger energy consumption since last known state of charge.
        charged_kwh = self.charger_used_energy_since(charger_energy_entity, last_updated) # Assumes no other vehicle has used the charger since *last_time*.
        self.log(f"Charged since {last_updated}: {charged_kwh:.2f} kWh")
        new_state_of_charge_kwh = state_of_charge_kwh + charged_kwh
        new_state_of_charge = new_state_of_charge_kwh / battery_size_kwh * 100

        return new_state_of_charge

    def charger_used_energy_since(self, charger_energy_entity: Entity, time: datetime) -> float:
        """Returns the energy consumed by the charger since the given time."""
        local_time = time.astimezone(tz.gettz('Europe/Stockholm')).replace(tzinfo=None)
        energy_history = self.get_history(entity_id=charger_energy_entity.entity_id, start_time=local_time)[0]
        # TODO: interpolate between
        if not energy_history:
            return 0

        earliest_state_after_time = float(energy_history[0]['state'])
        latest_state = float(energy_history[-1]['state'])
        energy_consumed = latest_state - earliest_state_after_time
        return energy_consumed

    def update_last_known_state_of_charge(self, entity, attribute, old, new, kwargs):
        # Try to convert the new state to a float.
        try:
            value = float(new)
        except ValueError:
            self.log(f"Could not convert {new} to float")
            return
        self.log(f"Updating last known state of charge to {new}")
        self.last_known_state_of_charge_entity.set_state(state=new)
