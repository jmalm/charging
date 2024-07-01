from enum import Enum

CHARGING = False
"True if a car is currently charging"


class Phase(Enum):
    """Represents a phase"""
    Unknown = 0
    P1 = 1
    P2 = 2
    P3 = 3


class Currents:
    """Represents the currents on each phase"""

    def __init__(self, p1, p2, p3):
        self._currents = {
            Phase.P1: p1,
            Phase.P2: p2,
            Phase.P3: p3
        }

    @property
    def p1(self):
        """Phase 1 current"""
        return self._currents[Phase.P1]

    @property
    def p2(self):
        """Phase 2 current"""
        return self._currents[Phase.P2]

    @property
    def p3(self):
        """Phase 3 current"""
        return self._currents[Phase.P3]

    def min(self):
        """Returns the minimum current"""
        return min(self._currents.values())

    def __str__(self):
        return f"P1: {self.p1} A, P2: {self.p2} A, P3: {self.p3} A"

    def __repr__(self):
        return f"Currents(p1={self.p1}, p2={self.p2}, p3={self.p3})"
