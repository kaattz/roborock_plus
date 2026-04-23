"""Safe-zone helpers for Roborock Plus."""

from __future__ import annotations

from dataclasses import asdict, dataclass
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

    def as_dict(self) -> dict[str, int]:
        """Convert the safe zone to a serializable mapping."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> "SafeZone":
        """Build a safe zone from stored data."""
        return cls(
            min_x=int(data["min_x"]),
            max_x=int(data["max_x"]),
            min_y=int(data["min_y"]),
            max_y=int(data["max_y"]),
        )


def _trigger_depth(safe_distance_front: int, safe_half_width: int, close_margin: int) -> int:
    """Return the forward depth of the suggested trigger box."""
    return min(safe_distance_front, max(safe_half_width, close_margin * 2))


def suggest_safe_zone(
    *,
    dock_x: int,
    dock_y: int,
    cabinet_direction: CabinetDirection,
    safe_distance_front: int,
    safe_half_width: int,
    close_margin: int,
) -> SafeZone:
    """Calculate a small trigger box near the dock exit."""
    trigger_depth = _trigger_depth(
        safe_distance_front=safe_distance_front,
        safe_half_width=safe_half_width,
        close_margin=close_margin,
    )
    if cabinet_direction == "north":
        return SafeZone(
            min_x=dock_x - safe_half_width,
            max_x=dock_x + safe_half_width,
            min_y=dock_y - trigger_depth - close_margin,
            max_y=dock_y - close_margin,
        )
    if cabinet_direction == "south":
        return SafeZone(
            min_x=dock_x - safe_half_width,
            max_x=dock_x + safe_half_width,
            min_y=dock_y + close_margin,
            max_y=dock_y + trigger_depth + close_margin,
        )
    if cabinet_direction == "west":
        return SafeZone(
            min_x=dock_x - trigger_depth - close_margin,
            max_x=dock_x - close_margin,
            min_y=dock_y - safe_half_width,
            max_y=dock_y + safe_half_width,
        )
    if cabinet_direction == "east":
        return SafeZone(
            min_x=dock_x + close_margin,
            max_x=dock_x + trigger_depth + close_margin,
            min_y=dock_y - safe_half_width,
            max_y=dock_y + safe_half_width,
        )
    raise ValueError("cabinet_direction must be one of north/south/east/west")


def point_in_safe_zone(x: int, y: int, zone: SafeZone) -> bool:
    """Return True if a point lies inside the safe zone."""
    return zone.min_x <= x <= zone.max_x and zone.min_y <= y <= zone.max_y
