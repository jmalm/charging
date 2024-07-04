from __future__ import annotations

from appdaemon.entity import Entity

from common import Currents


class Charger:
    def __init__(self, status: Entity, current: Entity | None, circuit_dynamic_limit: Entity | None):
        self._status = status
        self._current = current
        self._circuit_dynamic_limit = circuit_dynamic_limit

    @property
    def max_charging_current(self) -> float:
        return float(self._status.attributes["circuit_ratedCurrent"])

    @property
    def main_fuse(self) -> float:
        return float(self._status.attributes["site_ratedCurrent"])

    @property
    def status(self) -> str:
        return self._status.state

    @property
    def circuit_id(self) -> str:
        return self._status.attributes['circuit_id']

    @property
    def current(self) -> float:
        if not self._current:
            raise Exception("Current entity not set")
        return float(self._current.state)

    @property
    def circuit_dynamic_limit(self) -> Currents:
        if not self._circuit_dynamic_limit:
            raise Exception("Circuit dynamic limit entity not set")
        current_state = self._circuit_dynamic_limit.attributes
        return Currents(current_state['state_dynamicCircuitCurrentP1'],
                        current_state['state_dynamicCircuitCurrentP2'],
                        current_state['state_dynamicCircuitCurrentP3'])
