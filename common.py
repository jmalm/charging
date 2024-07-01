from enum import Enum

CHARGING = False
"True if a car is currently charging"


class Phase(Enum):
    Unknown = 0
    P1 = 1
    P2 = 2
    P3 = 3
