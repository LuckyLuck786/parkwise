"""Geometry helpers.

The campus is a flat metric plane in metres rather than lat/lng: the bay editor
needs a real grid the thermocol model can mirror, and haversine would only add
rounding noise at this scale. Keeping it 2-D also means the backend needs no
PostGIS.
"""

from __future__ import annotations

import math

#: Assumed average driving speed on campus access roads, metres per second.
DRIVE_SPEED_MPS = 6.0
WALK_SPEED_MPS = 1.35


def euclidean_m(
    x1: float, y1: float, x2: float, y2: float, *, scale: float = 1.0
) -> float:
    """Straight-line distance in metres between two campus-plane points."""
    return math.hypot(x2 - x1, y2 - y1) * scale


def walking_m(
    bay_x: float,
    bay_y: float,
    building_x: float,
    building_y: float,
    *,
    detour: float = 1.25,
) -> float:
    """Walking distance from a bay to its building.

    ``detour`` is a fixed 1.25 factor standing in for "you cannot walk in a
    straight line through a building". It is an assumption, not a measurement,
    and the UI says so where the number is shown.
    """
    return euclidean_m(bay_x, bay_y, building_x, building_y) * detour


def walking_minutes(distance_m: float, speed_mps: float = WALK_SPEED_MPS) -> float:
    return round(distance_m / max(0.1, speed_mps) / 60.0, 1)


def drive_minutes(road_distance_m: float, speed_mps: float = DRIVE_SPEED_MPS) -> float:
    return round(road_distance_m / max(0.5, speed_mps) / 60.0, 1)
