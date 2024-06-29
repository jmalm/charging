from enum import Enum
import appdaemon.plugins.hass.hassapi as hass

import common


class Phase(Enum):
    Unknown = 0
    P1 = 1
    P2 = 2
    P3 = 3


class LoadBalancer(hass.Hass):
    enabled = False
    one_phase_charging = False
    main_fuse_A = 0  # Refuse to guess
    min_charging_current = 6  # A
    current_l1_entity = None
    current_l2_entity = None
    current_l3_entity = None
    charging_phase = None
    charger_current_entity = None
    dynamic_circuit_limit_entity = None
    max_charging_current = 16  # A

    def initialize(self):
        # Should we do load balancing?
        do_load_balancing_entity_id = str(self.args['load_balancing_entity_id'])
        self.enabled = self.get_state(do_load_balancing_entity_id) == 'on'
        self.listen_state(self.load_balancing_cb, do_load_balancing_entity_id)

        # Should we do one-phase charging (and load balancing)?
        do_one_phase_charging_entity_id = str(self.args['one_phase_charging_entity_id'])
        self.one_phase_charging = self.get_state(do_one_phase_charging_entity_id) == 'on'
        self.listen_state(self.one_phase_charging_cb, do_one_phase_charging_entity_id)

        self.main_fuse_A = int(self.args['main_fuse_A'])

        # Instantaneous current readings
        current_l1_entity_id = str(self.args['current_l1_entity_id'])
        current_l2_entity_id = str(self.args['current_l2_entity_id'])
        current_l3_entity_id = str(self.args['current_l3_entity_id'])
        self.current_l1_entity = self.get_entity(current_l1_entity_id)
        self.current_l2_entity = self.get_entity(current_l2_entity_id)
        self.current_l3_entity = self.get_entity(current_l3_entity_id)
        self.listen_state(self.balance, current_l1_entity_id)
        self.listen_state(self.balance, current_l2_entity_id)
        self.listen_state(self.balance, current_l3_entity_id)

        # Charger status
        charger_current_entity_id = str(self.args['charger_current_entity_id'])
        self.charger_current_entity = self.get_entity(charger_current_entity_id)

        # Circuit dynamic current limit
        dynamic_circuit_limit_entity_id = str(self.args['dynamic_circuit_limit_entity_id'])
        self.dynamic_circuit_limit_entity = self.get_entity(dynamic_circuit_limit_entity_id)

    def load_balancing_cb(self, entity, attribute, old, new, kwargs):
        self.enabled = new == 'on'

    def one_phase_charging_cb(self, entity, attribute, old, new, kwargs):
        self.one_phase_charging = new == 'on'

    def balance(self, entity, attribute, old, new, kwargs):
        l1 = float(self.current_l1_entity.state)
        l2 = float(self.current_l2_entity.state)
        l3 = float(self.current_l3_entity.state)

        if l1 > self.main_fuse_A:
            self.log(f"L1 current is higher than main fuse: {l1}", level="WARNING")

        if l2 > self.main_fuse_A:
            self.log(f"L2 current is higher than main fuse: {l2}", level="WARNING")

        if l3 > self.main_fuse_A:
            self.log(f"L3 current is higher than main fuse: {l3}", level="WARNING")

        if not self.enabled:
            self.log(f"Load balancing is disabled.", level="INFO")
            return

        load_balance_threshold = self.main_fuse_A * 0.9  # Balance load when current is higher than 90% of main fuse.
        above_threshold = l1 > load_balance_threshold or l2 > load_balance_threshold or l3 > load_balance_threshold

        # Get the dynamic circuit limit for each phase.
        self.dynamic_circuit_limit_entity.get_state()  # Update entity.
        dynamic_circuit_limit = {
            Phase.P1: float(self.dynamic_circuit_limit_entity.attributes['state_dynamicCircuitCurrentP1']),
            Phase.P2: float(self.dynamic_circuit_limit_entity.attributes['state_dynamicCircuitCurrentP2']),
            Phase.P3: float(self.dynamic_circuit_limit_entity.attributes['state_dynamicCircuitCurrentP3']),
        }
        self.log(f"Dynamic circuit limit: {dynamic_circuit_limit}", level="INFO")

        min_dynamic_circuit_limit = min(dynamic_circuit_limit.values())
        if not common.CHARGING and min_dynamic_circuit_limit >= self.max_charging_current:
            self.log(f"Not charging, so no need to load balance.", level="INFO")
            return

        charger_current = float(self.charger_current_entity.state)

        if not above_threshold and min_dynamic_circuit_limit >= self.max_charging_current:
            # The charging is not limited, and we're still not over the main fuse. Nothing to do.
            self.log("Charger is charging, but not limited, "
                     f"and no phase is loaded above the threshold for load balancing ({load_balance_threshold} A).",
                     level="INFO")
            return

        if self.one_phase_charging:
            self.balance_one_phase(l1, l2, l3, charger_current)
        else:
            self.balance_three_phase(l1, l2, l3, charger_current)

    def balance_one_phase(self, l1, l2, l3, charger_current):
        charging_phase = Phase.Unknown
        if charger_current >= self.min_charging_current:
            # The charger is currently charging a vehicle - but on which phase?
            charging_phase = self.get_charging_phase(l1, l2, l3)
        self.log(f"Charging phase: {charging_phase.name}", level="INFO")

        # Figure out the load on each phase, without the charger.
        other_load = {Phase.P1: l1,
                      Phase.P2: l2,
                      Phase.P3: l3}
        if charging_phase != Phase.Unknown:
            other_load[charging_phase] -= charger_current
        self.log(f"Other load: {other_load}", level="INFO")

        # Which phase has the lowest other load?
        min_load_phase = min(other_load, key=other_load.get)
        self.log(f"Min load phase: {min_load_phase.name}", level="INFO")

        # Is the charger charging on the same phase?
        # If not, should we switch charging phase? Maybe look at the past 15 or so minutes?

        # Do we need to adjust the circuit current limit?

    def balance_three_phase(self, l1, l2, l3, charger_current):
        raise NotImplementedError("Three-phase load balancing not yet implemented.")

    def get_charging_phase(self, l1, l2, l3):
        if self.charging_phase is not Phase.Unknown:
            # We already know the charging phase.
            return self.charging_phase

        # We don't know yet. Let's guess based on current readings.
        # If the charger is charging a vehicle, let's assume it is charging on the phase with the highest load.

        if not common.CHARGING:
            # The charger is not charging a vehicle. We can't guess the charging phase.
            return Phase.Unknown

        if l1 > l2 and l1 > l3:
            # L1 is the highest current.
            return Phase.P1
        elif l2 > l1 and l2 > l3:
            # L2 is the highest current.
            return Phase.P2
        elif l3 > l1 and l3 > l2:
            # L3 is the highest current.
            return Phase.P3

        return Phase.Unknown
