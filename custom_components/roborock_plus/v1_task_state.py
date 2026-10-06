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
