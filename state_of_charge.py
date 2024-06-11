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

        self.charger_energy_entity = self.get_entity(self.charger_energy_entity_id)
        self.last_known_state_of_charge_entity = self.get_entity(self.last_known_state_of_charge_entity_id)
        self.estimated_state_of_charge_entity = self.get_entity(self.estimated_state_of_charge_entity_id)
    
        # Register callbacks
        self.listen_state(self.estimate, self.charger_energy_entity_id)
        self.listen_state(self.estimate, self.last_known_state_of_charge_entity_id)


    def estimate(self, entity, attribute, old, new, kwargs):
        estimated_soc = self.estimate_state_of_charge(float(self.last_known_state_of_charge_entity.state),
                                                 parser.parse(self.last_known_state_of_charge_entity.last_changed),
                                                 float(self.battery_size_kWh),
                                                 self.charger_energy_entity)
        self.estimated_state_of_charge_entity.set_state(state=round(estimated_soc))

    def estimate_state_of_charge(self, known_state_of_charge: float, last_updated: datetime, battery_size_kWh: float, charger_energy_entity: Entity) -> float:
        """Estimate the state of charge right now, based on last known state of charge and the charger energy consumption."""
        state_of_charge_kWh = known_state_of_charge / 100 * battery_size_kWh
        
        # Add charger energy consumption since last known state of charge.
        charged_kWh = self.charger_used_energy_since(charger_energy_entity, last_updated) # Assumes no other vehicle has used the charger since *last_time*.
        self.log(f"Charged since {last_updated}: {charged_kWh} kWh")
        new_state_of_charge_kWh = state_of_charge_kWh + charged_kWh
        new_state_of_charge = new_state_of_charge_kWh / battery_size_kWh * 100

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