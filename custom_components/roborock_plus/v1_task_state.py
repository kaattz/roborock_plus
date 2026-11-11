"""Helpers for V1 vacuum task state."""

from __future__ import annotations

from typing import Any

_TASK_ACTIVE_STATE_NAMES = {
    "starting",
    "remote_control_active",
    "cleaning",
    "manual_mode",
    "paused",
    "spot_cleaning",
    "docking",
    # `returning_home` is the outbound leg of the same drive home that `docking`
    # covers the end of. Leaving it out made `task_active` fall off several
    # seconds before the robot reached the dock -- measured 2026-11-10
    # 15:09:21 (`task_active` off, still `returning`) -> 15:09:26 (`docked`),
    # and the same 4 s and 11 s gaps on 2026-11-08. `in_returning` covers the
    # usual case, but it is a separate reading and can clear first; a consumer
    # that samples once in that window sees "task over, robot not home" and has
    # no way to tell it apart from a robot that stopped in the room.
    "returning_home",
    "going_to_target",
    "zoned_cleaning",
    "segment_cleaning",
    "emptying_the_bin",
    "washing_the_mop",
    "going_to_wash_the_mop",
}


def is_v1_task_active(
    *,
    state: Any,
    in_cleaning: bool | int | None,
    in_returning: bool | int | None,
) -> bool:
    """Return whether a clean/dock task is still active."""
    if bool(in_cleaning) or bool(in_returning):
        return True
    return _state_name(state) in _TASK_ACTIVE_STATE_NAMES


def _state_name(state: Any) -> str:
    raw_name = getattr(state, "name", state)
    return str(raw_name)
