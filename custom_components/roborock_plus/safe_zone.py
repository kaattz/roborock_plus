"""Safe-zone helpers for Roborock Plus."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CabinetDirection = Literal["north", "south", "east", "west"]

DEFAULT_DOCK_X = 25500
DEFAULT_DOCK_Y = 25500


@dataclass(frozen=True)
class SafeZone:
    """Rectangle that is safe for closing the garage door."""

    min_x: int
    max_x: int
    min_y: int
    max_y: int


def suggest_safe_zone(
    *,
    dock_x: int,
    dock_y: int,
    cabinet_direction: CabinetDirection,
    safe_distance_front: int,
    safe_half_width: int,
    close_margin: int,
) -> SafeZone:
    """Calculate a conservative rectangular safe zone."""
    if cabinet_direction == "north":
        return SafeZone(
            min_x=dock_x - safe_half_width,
            max_x=dock_x + safe_half_width,
            min_y=dock_y - safe_distance_front - close_margin,
            max_y=dock_y - close_margin,
        )
    if cabinet_direction == "south":
        return SafeZone(
            min_x=dock_x - safe_half_width,
            max_x=dock_x + safe_half_width,
            min_y=dock_y + close_margin,
            max_y=dock_y + safe_distance_front + close_margin,
        )
    if cabinet_direction == "west":
        return SafeZone(
            min_x=dock_x - safe_distance_front - close_margin,
            max_x=dock_x - close_margin,
            min_y=dock_y - safe_half_width,
            max_y=dock_y + safe_half_width,
        )
    if cabinet_direction == "east":
        return SafeZone(
            min_x=dock_x + close_margin,
            max_x=dock_x + safe_distance_front + close_margin,
            min_y=dock_y - safe_half_width,
            max_y=dock_y + safe_half_width,
        )
    raise ValueError("cabinet_direction must be one of north/south/east/west")


def point_in_safe_zone(x: int, y: int, zone: SafeZone) -> bool:
    """Return True if a point lies inside the safe zone."""
    return zone.min_x <= x <= zone.max_x and zone.min_y <= y <= zone.max_y
