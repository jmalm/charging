from __future__ import annotations

from math import floor

import appdaemon.plugins.hass.hassapi as hass

from charger import Charger
from common import Phase, Currents


class LoadBalancer(hass.Hass):
    """App for making sure that the charger is not overloaded."""
    load_balancing_enabled = False
    one_phase_charging = False
    charge_now_switch = False
    smart_charge = False
    charger = None
    load_balance_threshold = 0
    min_charging_current = 6  # A
    current_l1_entity = None
    current_l2_entity = None
    current_l3_entity = None
    circuit_dynamic_limit_target: Currents | None = None

    def initialize(self):
        # Should we do load balancing?
        do_load_balancing_entity_id = str(self.args['load_balancing_entity_id'])
        self.load_balancing_enabled = self.get_state(do_load_balancing_entity_id) == 'on'
        self.listen_state(self.load_balancing_cb, do_load_balancing_entity_id)

        # Shall we do smart charging?
        smart_charging_entity_id = str(self.args['smart_charging_entity_id'])
        self.smart_charge = self.get_state(smart_charging_entity_id) == 'on'
        self.listen_state(self.smart_charging_cb, smart_charging_entity_id)

        # Should we do one-phase charging (and load balancing)?
        do_one_phase_charging_entity_id = str(self.args['one_phase_charging_entity_id'])
        self.one_phase_charging = self.get_state(do_one_phase_charging_entity_id) == 'on'
        assert self.one_phase_charging, "Three-phase charging is not supported yet"
        self.listen_state(self.one_phase_charging_cb, do_one_phase_charging_entity_id)

        # Shall charging be on now?
        charge_now_entity_id = str(self.args['charge_now_entity_id'])
        self.charge_now_switch = self.get_state(charge_now_entity_id) == 'on'
        self.listen_state(self.charge_now_cb, charge_now_entity_id)

        # Charger
        charger_status_entity_id = str(self.args['charger_status_entity_id'])
        charger_current_entity_id = str(self.args['charger_current_entity_id'])
        circuit_dynamic_limit_entity_id = str(self.args['circuit_dynamic_limit_entity_id'])
        self.charger = Charger(self.get_entity(charger_status_entity_id),
                               self.get_entity(charger_current_entity_id),
                               self.get_entity(circuit_dynamic_limit_entity_id))

        # Balance load when current is higher than 90% of main fuse.
        self.load_balance_threshold = self.charger.main_fuse * 0.9

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

        self.balance()

    def load_balancing_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the load balancing switch."""
        self.load_balancing_enabled = new == 'on'
        self.log(f"Load balancing: {new}")
        self.balance()

    def smart_charging_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the smart charging switch."""
        self.smart_charge = new == 'on'
        self.log(f"Smart charging: {new}")
        self.log(f"Charge now: {self.charge_now} (Charge now switch={self.charge_now_switch})")
        self.balance()

    def one_phase_charging_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the one phase charging switch."""
        self.one_phase_charging = new == 'on'
        self.log(f"One phase charging: {new}")
        assert self.one_phase_charging, "Three-phase charging or load balancing is not supported yet"
        self.balance()

    def charge_now_cb(self, entity, attribute, old, new, kwargs):
        """Callback for the charge now switch."""
        self.charge_now_switch = new == 'on'
        self.log(f"Charge now switch: {new}")
        self.log(f"Charge now: {self.charge_now} (Smart charge={self.smart_charge})")
        self.balance()

    @property
    def charge_now(self):
        return self.charge_now_switch or not self.smart_charge

    def balance(self, *args, **kwargs):
        """Make sure that the currents are not higher than the main fuse."""
        if not self.circuit_dynamic_limit_target_reached():
            # Circuit dynamic limit is being set. Wait for it to be reached.
            return

        if not self.load_balancing_enabled:
            self.handle_non_balanced_charging()

        l1 = float(self.current_l1_entity.state)
        l2 = float(self.current_l2_entity.state)
        l3 = float(self.current_l3_entity.state)
        load = Currents(l1, l2, l3)
        above_threshold = load.max() > self.load_balance_threshold

        if l1 > self.charger.main_fuse:
            self.log(f"L1 current is higher than main fuse: {l1}", level="WARNING")

        if l2 > self.charger.main_fuse:
            self.log(f"L2 current is higher than main fuse: {l2}", level="WARNING")

        if l3 > self.charger.main_fuse:
            self.log(f"L3 current is higher than main fuse: {l3}", level="WARNING")

        if not self.load_balancing_enabled:
            self.log(f"Load balancing is disabled.", level="DEBUG")
            return

        # Get the circuit dynamic limit for each phase.
        circuit_dynamic_limit = self.charger.circuit_dynamic_limit
        self.log(f"Circuit dynamic limit: {circuit_dynamic_limit}", level="DEBUG")

        min_circuit_dynamic_limit = circuit_dynamic_limit.min()

        if not self.charge_now:
            # Charging is off. Make sure that circuit dynamic limit is set to 0 A.
            if circuit_dynamic_limit.max() >= self.min_charging_current:
                self.log("Should not charge now but circuit dynamic limit currently allows it"
                         f" ({circuit_dynamic_limit}) - setting limit to 0 A", level="INFO")
                self.set_circuit_dynamic_limit(Currents(0, 0, 0))
            return

        if not above_threshold and min_circuit_dynamic_limit >= self.charger.max_charging_current:
            # The charging is not limited, and we're still not over the main fuse. Nothing to do.
            self.log("Charging is enabled without limitation, "
                     f"and no phase is loaded above the threshold for load balancing ({self.load_balance_threshold} A). "
                     "Nothing to do.", level="DEBUG")
            return

        if self.one_phase_charging:
            self.balance_one_phase(load, self.charger.current)
        else:
            # TODO: Not sure what self.charger_current is when doing three-phase charging.
            self.balance_three_phase(l1, l2, l3, self.charger.current)

    def balance_one_phase(self, load, charger_current):
        """Balance the load when the charger is set to only charge on one phase."""
        charging_phase = self.get_charging_phase()
        if charger_current >= self.min_charging_current:
            self.log(f"Charging with {charger_current} A on phase {charging_phase.name}", level="INFO")

        # Figure out the load on each phase, without the charger.
        other_load = Currents(load.p1, load.p2, load.p3)
        if charging_phase != Phase.Unknown:
            other_load[charging_phase] -= charger_current
        self.log(f"Other load: {other_load}", level="DEBUG")

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
        new_circuit_dynamic_limit[charging_phase] = min(floor(available_current), self.charger.max_charging_current)
        current_circuit_dynamic_limit = self.charger.circuit_dynamic_limit
        if new_circuit_dynamic_limit.max() < current_circuit_dynamic_limit.max():
            self.log(f"Lowering circuit dynamic limit: {new_circuit_dynamic_limit}", level="INFO")
        elif new_circuit_dynamic_limit.max() >= current_circuit_dynamic_limit.max() + 2:  # Hysteresis 2 A
            self.log(f"Raising circuit dynamic limit: {new_circuit_dynamic_limit}", level="INFO")
        else:
            return
        self.set_circuit_dynamic_limit(new_circuit_dynamic_limit)

        # Is the charger charging on the same phase?
        # If not, should we switch charging phase? Maybe look at the past 15 or so minutes?

    def balance_three_phase(self, l1, l2, l3, charger_current):
        """Balance the load when the charger is set to charge on all three phases."""
        raise NotImplementedError("Three-phase load balancing not yet implemented.")

    def get_charging_phase(self):
        """Get the phase that charging is enabled on."""
        # Do we have a specific phase enabled by circuit dynamic limit?
        current_limit = self.charger.circuit_dynamic_limit
        if self.min_charging_current < current_limit.max() == current_limit.p1 + current_limit.p2 + current_limit.p3:
            # Charging is enabled on one specific phase.
            return current_limit.max_phase()

        if not self.charge_now:
            # The charger is not charging a vehicle. We can't guess the charging phase.
            return Phase.Unknown

        # TODO: Get it from the attributes of circuit_current_entity.

        return Phase.Unknown

    def set_circuit_dynamic_limit(self, currents: Currents):
        """Set the circuit dynamic limit."""
        if currents == self.charger.circuit_dynamic_limit or not self.circuit_dynamic_limit_target_reached():
            return
        self.log(f"Setting circuit dynamic limit to {currents}.", level="INFO")
        self.call_service('easee/set_circuit_dynamic_limit',
                          circuit_id=self.charger.circuit_id,
                          currentP1=currents.p1,
                          currentP2=currents.p2,
                          currentP3=currents.p3)
        self.circuit_dynamic_limit_target = currents

    def circuit_dynamic_limit_target_reached(self) -> bool:
        """Check if the circuit dynamic limit target is reached."""
        if self.circuit_dynamic_limit_target is None:
            return True

        current_limit = self.charger.circuit_dynamic_limit
        if (current_limit.p1 == self.circuit_dynamic_limit_target.p1 and
                current_limit.p2 == self.circuit_dynamic_limit_target.p2 and
                current_limit.p3 == self.circuit_dynamic_limit_target.p3):
            self.log(f"Circuit dynamic limit is now set to {self.circuit_dynamic_limit_target}.",
                     level="INFO")
            self.circuit_dynamic_limit_target = None
            return True
        self.log(f"Circuit dynamic limit is being set to {self.circuit_dynamic_limit_target}.",
                 level="DEBUG")
        return False

    def handle_non_balanced_charging(self):
        if self.charge_now:
            target_limit = Currents(40, 40, 40)
            if self.charger.circuit_dynamic_limit != target_limit:
                self.log(
                    f"Load balancing is disabled. Enabling charging by resetting circuit dynamic limit: {target_limit}",
                    level="INFO")
                self.set_circuit_dynamic_limit(target_limit)
        else:
            target_limit = Currents(0, 0, 0)
            if self.charger.circuit_dynamic_limit != target_limit:
                self.log(
                    f"Load balancing is disabled. Disabling charging by setting circuit dynamic limit: {target_limit}",
                    level="INFO")
                self.set_circuit_dynamic_limit(target_limit)
