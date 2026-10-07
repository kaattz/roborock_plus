"""Detect a vacuum that should be moving but is not.

The cabinet-door automations cover the robot leaving the dock and the robot
returning to it. Neither notices a robot that is wedged against a closed door,
or one that cannot reach the dock because an open door leaf is in the way: in
both cases the task stays active, so nothing in Home Assistant reacts.

The position needed to notice that lives in the integration (there is no
position entity to watch), so the fact is computed here and reported. Acting on
it -- stopping the robot, notifying someone -- stays in automations, where the
user can add their own conditions and see the decision in a trace.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

DOMAIN = "roborock_plus"

EVENT_VACUUM_STUCK = f"{DOMAIN}_vacuum_stuck"

CONF_V1_STUCK_DETECTION_ENABLED = "v1_stuck_detection_enabled"
CONF_V1_STUCK_WINDOW = "v1_stuck_window"
CONF_V1_STUCK_RADIUS = "v1_stuck_radius"

# Long enough that a slow turn or a careful pass along an edge is not mistaken
# for being stuck, short enough to speak up while the robot is still pressed
# against whatever stopped it.
DEFAULT_V1_STUCK_WINDOW = 120
MIN_V1_STUCK_WINDOW = 30
MAX_V1_STUCK_WINDOW = 600

# Two samples taken while the robot sat still measured (25728, 24233) and
# (25727, 24232), so a radius has to clear that jitter. It also has to stay far
# below the ~900 units the robot covers in three seconds at cleaning speed, or
# real movement would look like standing still.
DEFAULT_V1_STUCK_RADIUS = 200
MIN_V1_STUCK_RADIUS = 50
MAX_V1_STUCK_RADIUS = 2000

# States in which the robot is expected to be travelling. Only these are
# assessed.
#
# This is a whitelist on purpose. A list of "legitimately idle" states would
# have to be complete, and every state it missed would raise a false alarm that
# stops a working clean. Naming only the states where movement is certain means
# an unlisted state is simply not assessed, which fails the safe way.
#
# Note this is deliberately *not* `is_v1_task_active`. That helper answers
# whether a task is still alive and so stays true while the robot pauses or
# washes the mop; using it here would report every mop wash and every mid-clean
# recharge as stuck.
MOVEMENT_STATES = frozenset(
    {
        "cleaning",
        "segment_cleaning",
        "zoned_cleaning",
        "spot_cleaning",
        "returning_home",
        "docking",
        "going_to_target",
        "going_to_wash_the_mop",
        "manual_mode",
        "remote_control_active",
        "mapping",
        "patrol",
        "robot_status_mopping",
        "clean_mop_cleaning",
        "clean_mop_mopping",
        "segment_mopping",
        "segment_clean_mop_cleaning",
        "segment_clean_mop_mopping",
        "zoned_mopping",
        "zoned_clean_mop_cleaning",
        "zoned_clean_mop_mopping",
        "back_to_dock_washing_duster",
    }
)


@dataclass(frozen=True)
class StuckOptions:
    """Resolved stuck-detection settings."""

    enabled: bool
    window: timedelta
    radius: float


def _state_name(state: Any) -> str:
    raw_name = getattr(state, "name", state)
    return str(raw_name)


def should_assess_movement(*, state: Any) -> bool:
    """Return whether the robot should be moving in this state."""
    return _state_name(state) in MOVEMENT_STATES


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def resolve_stuck_options(options: Mapping[str, Any] | None) -> StuckOptions:
    """Return the resolved settings, clamping unusable values to the defaults.

    An out-of-range value is clamped rather than rejected so a typo cannot
    silently disable the detector, and a nonsensical one falls back to the
    default for the same reason.
    """
    raw = options or {}
    enabled = raw.get(CONF_V1_STUCK_DETECTION_ENABLED, True)

    window = _as_int(raw.get(CONF_V1_STUCK_WINDOW), DEFAULT_V1_STUCK_WINDOW)
    window = max(MIN_V1_STUCK_WINDOW, min(window, MAX_V1_STUCK_WINDOW))

    radius = _as_int(raw.get(CONF_V1_STUCK_RADIUS), DEFAULT_V1_STUCK_RADIUS)
    radius = max(MIN_V1_STUCK_RADIUS, min(radius, MAX_V1_STUCK_RADIUS))

    return StuckOptions(
        enabled=bool(enabled),
        window=timedelta(seconds=window),
        radius=float(radius),
    )


@dataclass
class _Anchor:
    """The position and sample time a movement measurement is taken against."""

    x: float
    y: float
    since: datetime


class StuckTracker:
    """Track whether the robot has stopped moving while it should be moving.

    Fed one observation per successful position sample. Kept free of Home
    Assistant imports so the timing rules can be tested directly.
    """

    def __init__(self, *, window: timedelta, radius: float) -> None:
        self._window = window
        self._radius = radius
        self._anchor: _Anchor | None = None
        self._stuck_since: datetime | None = None
        self._last_sample_time: datetime | None = None

    @property
    def is_stuck(self) -> bool:
        """Return whether the robot is currently believed to be stuck."""
        return self._stuck_since is not None

    def seconds_stuck(self, now: datetime) -> float | None:
        """Return how long the robot has been stuck, if it is."""
        if self._stuck_since is None:
            return None
        return (now - self._stuck_since).total_seconds()

    def reset(self) -> None:
        """Forget the current window and any stuck verdict."""
        self._anchor = None
        self._stuck_since = None
        self._last_sample_time = None

    def observe(
        self,
        *,
        now: datetime,
        sample_time: datetime | None,
        x: float | None,
        y: float | None,
        should_move: bool,
    ) -> bool:
        """Feed one observation and return whether the robot is stuck.

        ``sample_time`` is when the position was actually read, not when this
        runs. A repeated sample is ignored rather than counted as a fresh
        "still not moving" reading: a failed map refresh leaves the previous
        position in place, and treating that as new evidence would turn a
        broken read into a stuck verdict.
        """
        if not should_move:
            # Idle, paused, washing the mop, charging: not moving is correct.
            self.reset()
            return False

        if x is None or y is None:
            # Nothing readable. Do not build a new verdict on missing data, and
            # do not discard one already reached: the robot has not been seen
            # to move, so a stuck verdict stands until it does.
            self._anchor = None
            self._last_sample_time = sample_time
            return self.is_stuck

        if sample_time is not None and sample_time == self._last_sample_time:
            return self.is_stuck
        self._last_sample_time = sample_time

        observed_at = sample_time or now

        if self._anchor is None:
            self._anchor = _Anchor(x=x, y=y, since=observed_at)
            return self.is_stuck

        if math.hypot(x - self._anchor.x, y - self._anchor.y) > self._radius:
            # It moved: start a new window from where it is now.
            self._anchor = _Anchor(x=x, y=y, since=observed_at)
            self._stuck_since = None
            return False

        if observed_at - self._anchor.since >= self._window:
            if self._stuck_since is None:
                self._stuck_since = self._anchor.since
            return True

        return self.is_stuck


def build_stuck_event_data(
    *,
    entity_id: str | None,
    x: float | None,
    y: float | None,
    state: Any,
    seconds_stuck: float | None,
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Build the event payload for a robot that stopped moving."""
    data: dict[str, Any] = {
        "state": _state_name(state),
        "seconds_stuck": int(seconds_stuck) if seconds_stuck is not None else None,
    }
    if entity_id:
        data["entity_id"] = entity_id
    if entry_id:
        data["entry_id"] = entry_id
    if x is not None:
        data["x"] = x
    if y is not None:
        data["y"] = y
    return data
