"""Helpers for V1 vacuum map position freshness.

The safe-zone entities (`in_safe_zone`, `clear_of_garage`) read the vacuum
position from the map content trait. That trait is refreshed less often than
the status trait, and a single failed refresh used to leave the entity serving
an old position. These helpers keep the position sampled aggressively while a
task is running and bound how long a sample may be trusted while it is.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# How often to re-read the map content while a task is active. The position is
# what decides whether it is safe to close the garage door, so this has to be
# well inside the automation's wait window.
MAP_POSITION_ACTIVE_INTERVAL = timedelta(seconds=10)

# While no task is running the robot is parked, so sampling can be cheaper.
MAP_POSITION_IDLE_INTERVAL = timedelta(seconds=60)

# A position sample older than this is treated as unknown while a task is
# running. This fails safe for `clear_of_garage`: a stale "robot is clear"
# reading must never be used to decide that closing the door is safe.
MAP_POSITION_MAX_AGE = timedelta(seconds=120)


def should_refresh_v1_map_position(
    *,
    now: datetime,
    last_attempt: datetime | None,
    task_active: bool,
    is_local_connected: bool,
    active_interval: timedelta = MAP_POSITION_ACTIVE_INTERVAL,
    idle_interval: timedelta = MAP_POSITION_IDLE_INTERVAL,
) -> bool:
    """Return whether the map position should be re-read on this poll.

    Only runs over a local connection: over the cloud the extra map reads would
    add rate-limit pressure for little benefit, so the caller keeps its existing
    map refresh cadence instead.
    """
    if not is_local_connected:
        return False
    if last_attempt is None:
        return True
    interval = active_interval if task_active else idle_interval
    return now - last_attempt >= interval


def is_v1_map_position_fresh(
    *,
    now: datetime,
    position_time: datetime | None,
    task_active: bool,
    max_age: timedelta = MAP_POSITION_MAX_AGE,
) -> bool:
    """Return whether a sampled position may be trusted for a safety decision.

    Only a running task can move the robot, so that is the only time an old
    sample is genuinely unsafe to act on. While no task is running the robot is
    parked and the last known position is still the best available answer,
    which keeps the idle-only cloud path from reporting `unknown` forever.
    """
    if not task_active:
        return True
    if position_time is None:
        return False
    return now - position_time <= max_age
