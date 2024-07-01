from __future__ import annotations

from math import floor

import appdaemon.plugins.hass.hassapi as hass

from common import Phase, Currents


class LoadBalancer(hass.Hass):
    """App for making sure that the charger is not overloaded."""
    enabled = False
    one_phase_charging = False
    charge_now = False
    main_fuse_A = 0  # Refuse to guess
    load_balance_threshold = 0
    min_charging_current = 6  # A
    current_l1_entity = None
    current_l2_entity = None
    current_l3_entity = None
    charger_status_entity = None
    charger_current_entity = None
    circuit_dynamic_limit_entity = None
    max_charging_current = 16  # A
    circuit_id = None
    circuit_dynamic_limit_target: Currents | None = None

    def initialize(self):
        # Should we do load balancing?
        do_load_balancing_entity_id = str(self.args['load_balancing_entity_id'])
        self.enabled = self.get_state(do_load_balancing_entity_id) == 'on'
        self.listen_state(self.load_balancing_cb, do_load_balancing_entity_id)

        # Should we do one-phase charging (and load balancing)?
        do_one_phase_charging_entity_id = str(self.args['one_phase_charging_entity_id'])
        self.one_phase_charging = self.get_state(do_one_phase_charging_entity_id) == 'on'
        assert self.one_phase_charging, "Three-phase charging is not supported yet"
        self.listen_state(self.one_phase_charging_cb, do_one_phase_charging_entity_id)

        # Shall charging be on now?
        charge_now_entity_id = str(self.args['charge_now_entity_id'])
        self.charge_now = self.get_state(charge_now_entity_id) == 'on'
        self.listen_state(self.charge_now_cb, charge_now_entity_id)

        self.main_fuse_A = int(self.args['main_fuse_A'])
        self.load_balance_threshold = self.main_fuse_A * 0.9  # Balance load when current is higher than 90% of main fuse.

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
        charger_status_entity_id = str(self.args['charger_status_entity_id'])
        self.charger_status_entity = self.get_entity(charger_status_entity_id)
        charger_current_entity_id = str(self.args['charger_current_entity_id'])
        self.charger_current_entity = self.get_entity(charger_current_entity_id)
        self.circuit_id = self.charger_status_entity.attributes['circuit_id']

        # Circuit dynamic current limit
        circuit_dynamic_limit_entity_id = str(self.args['circuit_dynamic_limit_entity_id'])
        self.circuit_dynamic_limit_entity = self.get_entity(circuit_dynamic_limit_entity_id)

        self.balance()

    def load_balancing_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the load balancing switch."""
        self.enabled = new == 'on'
        self.balance()

    def one_phase_charging_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the one phase charging switch."""
        self.one_phase_charging = new == 'on'
        assert self.one_phase_charging, "Three-phase charging is not supported yet"
        self.balance()

    def charge_now_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the charge now switch."""
        self.charge_now = new == 'on'
        self.log(f"Charge now: {self.charge_now}")
        self.balance()

    def balance(self, *args, **kwargs):
        """Make sure that the currents are not higher than the main fuse."""
        if not self.circuit_dynamic_limit_target_reached():
            return

        l1 = float(self.current_l1_entity.state)
        l2 = float(self.current_l2_entity.state)
        l3 = float(self.current_l3_entity.state)
        load = Currents(l1, l2, l3)

        if l1 > self.main_fuse_A:
            self.log(f"L1 current is higher than main fuse: {l1}", level="WARNING")

        if l2 > self.main_fuse_A:
            self.log(f"L2 current is higher than main fuse: {l2}", level="WARNING")

        if l3 > self.main_fuse_A:
            self.log(f"L3 current is higher than main fuse: {l3}", level="WARNING")

        if not self.enabled:
            self.log(f"Load balancing is disabled.", level="INFO")
            return

        above_threshold = l1 > self.load_balance_threshold or l2 > self.load_balance_threshold or l3 > self.load_balance_threshold

        # Get the circuit dynamic limit for each phase.
        circuit_dynamic_limit = self.get_circuit_dynamic_limit()
        self.log(f"Circuit dynamic limit: {circuit_dynamic_limit}", level="DEBUG")

        min_circuit_dynamic_limit = circuit_dynamic_limit.min()

        if not self.charge_now:
            # Charging is off. Make sure that circuit dynamic limit is set to 0 A.
            if circuit_dynamic_limit.max() >= self.min_charging_current:
                self.log("Should not charge now but circuit dynamic limit currently allows it"
                         f" ({circuit_dynamic_limit}) - setting limit to 0 A", level="INFO")
                self.set_circuit_dynamic_limit(Currents(0, 0, 0))
            return

        # TODO: Not sure what this is when doing three-phase charging.
        charger_current = float(self.charger_current_entity.state)

        if not above_threshold and min_circuit_dynamic_limit >= self.max_charging_current:
            # The charging is not limited, and we're still not over the main fuse. Nothing to do.
            self.log("Charger is charging without limitation, "
                     f"and no phase is loaded above the threshold for load balancing ({self.load_balance_threshold} A). "
                     "Nothing to do.", level="INFO")
            return

        if self.one_phase_charging:
            self.balance_one_phase(l1, l2, l3, charger_current)
        else:
            self.balance_three_phase(l1, l2, l3, charger_current)

    def balance_one_phase(self, l1, l2, l3, charger_current):
        """Balance the load when the charger is set to only charge on one phase."""
        charging_phase = self.get_charging_phase(l1, l2, l3)
        if charger_current >= self.min_charging_current:
            self.log(f"Charging with {charger_current} A on phase {charging_phase.name}", level="INFO")

        # Figure out the load on each phase, without the charger.
        other_load = Currents(l1, l2, l3)
        if charging_phase != Phase.Unknown:
            other_load[charging_phase] -= charger_current
        self.log(f"Other load: {other_load}", level="INFO")

        # Which phase has the lowest other load?
        min_load_phase = other_load.min_phase()
        self.log(f"Min load phase: {min_load_phase.name}", level="DEBUG")

        if charging_phase == Phase.Unknown:
            charging_phase = min_load_phase
            self.log(f"Enabling charging on the phase with the lowest load: {charging_phase.name}",
                     level="INFO")
        else:
            self.log(f"Charging is already enabled on phase {charging_phase.name}", level="DEBUG")
        available_current = self.load_balance_threshold - other_load[charging_phase]
        new_circuit_dynamic_limit = Currents(0, 0, 0)
        new_circuit_dynamic_limit[charging_phase] = floor(available_current)
        current_circuit_dynamic_limit = self.get_circuit_dynamic_limit()
        if new_circuit_dynamic_limit.max() < current_circuit_dynamic_limit.max():
            self.log(f"Lowering circuit dynamic limit: {new_circuit_dynamic_limit}", level="INFO")
        elif new_circuit_dynamic_limit.max() >= current_circuit_dynamic_limit.max() + 2:  # Hysteresis 2 A
            self.log(f"Raising circuit dynamic limit: {new_circuit_dynamic_limit}", level="INFO")
        else:
            return
        self.set_circuit_dynamic_limit(new_circuit_dynamic_limit)

        # Is the charger charging on the same phase?
        # If not, should we switch charging phase? Maybe look at the past 15 or so minutes?

        # Do we need to adjust the circuit current limit?

    def balance_three_phase(self, l1, l2, l3, charger_current):
        """Balance the load when the charger is set to charge on all three phases."""
        raise NotImplementedError("Three-phase load balancing not yet implemented.")

    def get_charging_phase(self, l1, l2, l3):
        """Get the phase that charging is enabled on."""
        # Do we have a specific phase enabled by circuit dynamic limit?
        current_limit = self.get_circuit_dynamic_limit()
        if self.min_charging_current < current_limit.max() == current_limit.p1 + current_limit.p2 + current_limit.p3:
            # Charging is enabled on one specific phase.
            return current_limit.max_phase()

        # We don't know yet. Let's guess based on current readings.
        # If the charger is charging a vehicle, let's assume it is charging on the phase with the highest load.

        if not self.charge_now:
            # The charger is not charging a vehicle. We can't guess the charging phase.
            return Phase.Unknown

        # if l1 > l2 and l1 > l3:
        #     # L1 is the highest current.
        #     return Phase.P1
        # elif l2 > l1 and l2 > l3:
        #     # L2 is the highest current.
        #     return Phase.P2
        # elif l3 > l1 and l3 > l2:
        #     # L3 is the highest current.
        #     return Phase.P3

        return Phase.Unknown

    def set_circuit_dynamic_limit(self, currents: Currents):
        """Set the circuit dynamic limit."""
        if currents == self.get_circuit_dynamic_limit() or not self.circuit_dynamic_limit_target_reached():
            return
        self.log(f"Setting circuit dynamic limit to {currents}.", level="INFO")
        self.call_service('easee/set_circuit_dynamic_limit',
                          circuit_id=self.circuit_id,
                          currentP1=currents.p1,
                          currentP2=currents.p2,
                          currentP3=currents.p3)
        self.circuit_dynamic_limit_target = currents

    def get_circuit_dynamic_limit(self) -> Currents:
        """Get the currently active circuit dynamic limit."""
        self.circuit_dynamic_limit_entity.get_state()
        current_state = self.circuit_dynamic_limit_entity.attributes
        return Currents(current_state['state_dynamicCircuitCurrentP1'],
                        current_state['state_dynamicCircuitCurrentP2'],
                        current_state['state_dynamicCircuitCurrentP3'])

    def circuit_dynamic_limit_target_reached(self) -> bool:
        """Check if the circuit dynamic limit target is reached."""
        if self.circuit_dynamic_limit_target is None:
            return True

        current_limits = self.get_circuit_dynamic_limit()
        if (current_limits.p1 == self.circuit_dynamic_limit_target.p1 and
                current_limits.p2 == self.circuit_dynamic_limit_target.p2 and
                current_limits.p3 == self.circuit_dynamic_limit_target.p3):
            self.log(f"Circuit dynamic limit is now set to {self.circuit_dynamic_limit_target}.",
                     level="INFO")
            self.circuit_dynamic_limit_target = None
            return True
        self.log(f"Circuit dynamic limit is being set to {self.circuit_dynamic_limit_target}.",
                 level="DEBUG")
        return False
