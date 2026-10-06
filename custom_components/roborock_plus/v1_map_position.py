"""Helpers for V1 vacuum map position freshness.

The safe-zone entities (`in_safe_zone`, `clear_of_garage`) read the vacuum
position from the map content trait. These helpers decide how often that trait
may be re-read and how long a sample may be trusted.

Important: map content does **not** travel over the local connection. In
python-roborock the trait is decorated `@common.map_rpc_channel`, which is
hard-wired to the MQTT channel with no local fallback. Every map read therefore
reaches whichever server the integration is pointed at. The polling budget is
a question about the *server*, not about whether the robot happens to be
reachable on the LAN, so the cadence is derived from the configured base URL.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

CONF_V1_MAP_POSITION_POLL_INTERVAL = "v1_map_position_poll_interval"

# 0 (the default) means "decide from the server": conservative against
# Roborock's own servers, fast against a self-hosted one.
DEFAULT_V1_MAP_POSITION_POLL_INTERVAL = 0
MIN_V1_MAP_POSITION_POLL_INTERVAL = 0
MAX_V1_MAP_POSITION_POLL_INTERVAL = 300

# Intervals used against Roborock's own servers, where going too fast risks
# rate limiting or a ban.
#
# Note the trade-off these values encode: the garage-door automations wait at
# most two minutes for `clear_of_garage` to flip after the robot leaves the
# dock. At a 60s interval the first fresh sample can be a full minute away, so
# against the official cloud the sensor is right at the edge of that window --
# and a single failed read pushes it past it. Local hosting is what makes the
# automation comfortable; the official-cloud values keep it correct-but-tight
# rather than risking the account.
MAP_POSITION_OFFICIAL_ACTIVE_INTERVAL = timedelta(seconds=60)
MAP_POSITION_OFFICIAL_IDLE_INTERVAL = timedelta(seconds=300)

# Even an explicit override may not poll Roborock's servers faster than this.
MAP_POSITION_OFFICIAL_MIN_INTERVAL = timedelta(seconds=30)

# Intervals used against a self-hosted server, where the only cost is local
# work. Fast sampling here is what removes the staleness that made
# `clear_of_garage` unusable.
MAP_POSITION_LOCAL_ACTIVE_INTERVAL = timedelta(seconds=10)
MAP_POSITION_LOCAL_IDLE_INTERVAL = timedelta(seconds=60)

# While idle, sample this many times less often than while a task is running.
MAP_POSITION_IDLE_MULTIPLIER = 6
MAP_POSITION_IDLE_FLOOR = timedelta(seconds=60)

# A sample stays usable for this many active intervals, so a single failed read
# does not immediately turn the entity into `unknown`. This is a safety bound:
# past it the position is too old to base a door-closing decision on. Two, not
# more: tolerating two consecutive misses would let a reading survive long
# enough for the robot to drive back into the zone and still be believed
# "clear", which is the one direction that must not fail open.
#
# There is deliberately no minimum age floor. A floor would silently decouple
# this from the sampling interval, so a fast self-hosted cadence could serve a
# position several samples out of date while still calling it fresh.
MAP_POSITION_MAX_AGE_INTERVALS = 2


def get_map_position_intervals(
    *,
    is_official_cloud: bool,
) -> tuple[timedelta, timedelta]:
    """Return the (active, idle) sampling intervals for the configured server."""
    if is_official_cloud:
        return (
            MAP_POSITION_OFFICIAL_ACTIVE_INTERVAL,
            MAP_POSITION_OFFICIAL_IDLE_INTERVAL,
        )
    return (
        MAP_POSITION_LOCAL_ACTIVE_INTERVAL,
        MAP_POSITION_LOCAL_IDLE_INTERVAL,
    )


def resolve_map_position_intervals(
    options: Mapping[str, Any],
    *,
    is_official_cloud: bool,
) -> tuple[timedelta, timedelta]:
    """Return the (active, idle) intervals, honouring an explicit override.

    An override is clamped rather than rejected, and can never make an official
    server be polled faster than `MAP_POSITION_OFFICIAL_MIN_INTERVAL`: a typo in
    a seconds field must not be able to get the account rate limited.
    """
    raw_interval = options.get(
        CONF_V1_MAP_POSITION_POLL_INTERVAL,
        DEFAULT_V1_MAP_POSITION_POLL_INTERVAL,
    )
    if not isinstance(raw_interval, int) or isinstance(raw_interval, bool):
        return get_map_position_intervals(is_official_cloud=is_official_cloud)
    if raw_interval <= 0:
        # Auto.
        return get_map_position_intervals(is_official_cloud=is_official_cloud)

    seconds = min(raw_interval, MAX_V1_MAP_POSITION_POLL_INTERVAL)
    active = timedelta(seconds=seconds)
    if is_official_cloud:
        active = max(active, MAP_POSITION_OFFICIAL_MIN_INTERVAL)
    idle = max(active * MAP_POSITION_IDLE_MULTIPLIER, MAP_POSITION_IDLE_FLOOR)
    return active, idle


def get_map_position_max_age(
    *,
    is_official_cloud: bool,
    active_interval: timedelta | None = None,
) -> timedelta:
    """Return how long a sample stays trustworthy for the configured server."""
    if active_interval is None:
        active_interval, _ = get_map_position_intervals(
            is_official_cloud=is_official_cloud
        )
    return active_interval * MAP_POSITION_MAX_AGE_INTERVALS


def should_refresh_v1_map_position(
    *,
    now: datetime,
    last_attempt: datetime | None,
    task_active: bool,
    is_official_cloud: bool,
    options: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether the map position should be re-read on this poll."""
    active_interval, idle_interval = resolve_map_position_intervals(
        options or {},
        is_official_cloud=is_official_cloud,
    )
    interval = active_interval if task_active else idle_interval
    if last_attempt is None:
        return True
    return now - last_attempt >= interval


def is_v1_map_position_fresh(
    *,
    now: datetime,
    position_time: datetime | None,
    task_active: bool,
    max_age: timedelta | None = None,
    is_official_cloud: bool = True,
    active_interval: timedelta | None = None,
) -> bool:
    """Return whether a sampled position may be trusted for a safety decision.

    Only a running task can move the robot, so that is the only time an old
    sample is genuinely unsafe to act on. While no task is running the robot is
    parked and the last known position is still the best available answer,
    which keeps a slow-polling setup from reporting `unknown` forever.
    """
    if not task_active:
        return True
    if position_time is None:
        return False
    if max_age is None:
        max_age = get_map_position_max_age(
            is_official_cloud=is_official_cloud,
            active_interval=active_interval,
        )
    return now - position_time <= max_age
