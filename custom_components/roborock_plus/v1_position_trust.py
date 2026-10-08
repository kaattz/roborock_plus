"""Decide whether a sampled vacuum position may be used for a safety decision.

The safe-zone entities decide whether the cabinet door may be closed from the
vacuum's position, which the integration reads out of the map payload. That
payload does not always describe where the robot actually is.

Measured on 2026-10-08, with the robot parked and charging since 01:26: a fresh
`map_content.refresh()` kept returning (25727, 24232) -- the living room -- for
more than thirteen hours, and the map image had not changed since 00:30. The
map's own charger marker sat at (25688, 28538), inside the configured danger
zone.

A stale position is dangerous in one specific direction. The robot is on the
dock, so it is standing in the danger zone, but the stale reading puts it in the
living room. `clear_of_garage` then reports that closing the door is clear --
which is the one answer that must never be wrong.

Two things made that possible beyond the payload itself:

* `is_v1_map_position_fresh` returns True whenever no task is active, so an
  arbitrarily old sample counted as current.
* `get_vacuum_current_position` reported `stale: false` because the read
  succeeded, not because the value was new.

This module supplies the missing corroboration. A robot in a charging state is
physically on its charger, so the map's charger marker is the better answer
whenever the two disagree. When there is no charger marker to corroborate
against, the position is refused rather than trusted: an unknown answer keeps
the door open, a wrong one closes it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# States in which the robot is sitting on its dock. `idle` is deliberately not
# here: it can mean parked on the dock or left in the middle of a room, and
# substituting the dock for a robot that is elsewhere would be its own error.
#
# `back_to_dock_washing_duster` (6310) is here despite being ambiguous: the name
# reads either as travelling to the dock or as sitting on it washing the duster.
# Treating it as docked is the fail-safe reading, because the two possible
# mistakes are not symmetric. If it is docked and we did not say so, the stale
# payload could place the robot outside the zone and the door could close on it.
# If it is travelling and we do say so, the door merely stays open a little
# longer, which is the direction that cannot hurt anyone.
#
# This set must stay disjoint from `v1_stuck_detection.MOVEMENT_STATES`: a state
# in both would feed a docked robot's unchanging position to the stuck detector
# and, once the window elapsed, raise a false "stuck" that stops a working clean.
# `test_position_trust_wiring` enforces the disjointness.
DOCKED_STATE_NAMES = frozenset(
    {
        "charging",
        "charging_complete",
        "charging_problem",
        "updating",
        "emptying_the_bin",
        "washing_the_mop",
        "washing_the_mop_2",
        "back_to_dock_washing_duster",
        "attaching_the_mop",
        "detaching_the_mop",
    }
)

# How far a docked robot's reported position may sit from the map's charger
# marker and still be the same place. Two samples of a parked robot differ by a
# unit or two, while the stale payload measured above was 4300 units out, so
# this separates the two cases with a wide margin on both sides.
DEFAULT_DOCK_TOLERANCE = 1000.0


@dataclass(frozen=True)
class ResolvedPosition:
    """A position cleared for use in a safety decision."""

    x: float | None
    y: float | None
    trusted: bool
    reason: str
    from_dock: bool = False


def _state_name(state: Any) -> str:
    raw_name = getattr(state, "name", state)
    return str(raw_name)


def is_docked_state(state: Any) -> bool:
    """Return whether the robot is sitting on its dock in this state."""
    return _state_name(state) in DOCKED_STATE_NAMES


def _coord(point: Any, axis: str) -> float | None:
    if point is None:
        return None
    value = getattr(point, axis, None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _distance(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def resolve_position_trust(
    *,
    state: Any,
    position: Any,
    charger: Any,
    dock_tolerance: float = DEFAULT_DOCK_TOLERANCE,
) -> ResolvedPosition:
    """Return the position to base a door decision on.

    While the robot is docked the charger marker wins, because that is where
    the robot physically is. A reported position that agrees with the marker is
    kept so the small sample-to-sample jitter is preserved; one that disagrees
    is replaced, since it is a leftover from an earlier task.
    """
    pos_x = _coord(position, "x")
    pos_y = _coord(position, "y")
    dock_x = _coord(charger, "x")
    dock_y = _coord(charger, "y")

    if not is_docked_state(state):
        if pos_x is None or pos_y is None:
            return ResolvedPosition(None, None, False, "missing_position")
        return ResolvedPosition(pos_x, pos_y, True, "not_docked")

    if dock_x is None or dock_y is None:
        # No corroboration available. Refuse rather than guess: an unknown
        # answer leaves the door open, a wrong one closes it on the robot.
        return ResolvedPosition(None, None, False, "docked_without_dock_reference")

    if pos_x is None or pos_y is None:
        return ResolvedPosition(
            dock_x, dock_y, True, "docked_position_from_charger", True
        )

    if _distance(pos_x, pos_y, dock_x, dock_y) <= dock_tolerance:
        return ResolvedPosition(pos_x, pos_y, True, "docked_at_charger")

    return ResolvedPosition(
        dock_x, dock_y, True, "docked_stale_position_replaced", True
    )
