"""Watch HA-initiated clean commands and report ones that never start.

Opening the garage door and issuing a clean command are separate steps, and
only the first is observable from Home Assistant. A routine is executed
asynchronously inside the server, so the HTTP call returns 200 even when the
routine then fails and the vacuum never moves. That leaves the door open with
no cleaning running and nothing to notice it.

This module closes that gap: after a clean command is issued, watch the task
state for a while and fire an event if no task ever starts. Reporting is left
to automations, so notifications stay under user control.

Only reporting is done here. Closing the door is deliberately not attempted:
a failed command does not prove the vacuum stayed put, and the door is shared
with other uses, so closing it could trap the robot or shut someone in.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

DOMAIN = "roborock_plus"

# Fired when a clean command was accepted but no task started within the
# timeout. Consumers (automations) decide how to notify.
EVENT_CLEAN_COMMAND_NOT_STARTED = f"{DOMAIN}_clean_command_not_started"

CONF_CLEAN_COMMAND_WATCH_TIMEOUT = "clean_command_watch_timeout"

# Long enough to cover the vacuum leaving the dock and reporting a task, short
# enough to speak up while the garage is still open.
DEFAULT_CLEAN_COMMAND_WATCH_TIMEOUT = 60

# A window this short would fire while the vacuum is still preparing, and one
# this long would outlast the open door it is meant to warn about.
MIN_CLEAN_COMMAND_WATCH_TIMEOUT = 10
MAX_CLEAN_COMMAND_WATCH_TIMEOUT = 600

WATCH_POLL_INTERVAL = 5.0


def resolve_clean_command_watch_timeout(options: dict[str, Any] | None) -> int:
    """Return the watch window in seconds, 0 when the watch is disabled.

    Unusable values fall back to the default rather than disabling the watch:
    silently losing the alert is worse than an unexpected window.
    """
    if not options:
        return DEFAULT_CLEAN_COMMAND_WATCH_TIMEOUT

    raw = options.get(CONF_CLEAN_COMMAND_WATCH_TIMEOUT)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_CLEAN_COMMAND_WATCH_TIMEOUT

    value = int(raw)
    if value <= 0:
        return 0
    return max(MIN_CLEAN_COMMAND_WATCH_TIMEOUT, min(value, MAX_CLEAN_COMMAND_WATCH_TIMEOUT))


async def async_watch_until_task_starts(
    *,
    timeout: float,
    is_task_active: Callable[[], bool | None],
    poll_interval: float = WATCH_POLL_INTERVAL,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait for a task to start, returning whether one did.

    ``is_task_active`` returns True once a task is running, False while the
    vacuum is still idle, and None while the state cannot be read yet.

    A run that never managed to read the state at all reports success: an
    unreadable state means the integration has a problem of its own, and
    blaming it on this command would raise a false alarm.
    """
    if timeout <= 0:
        return True

    deadline = monotonic() + timeout
    saw_state = False

    while True:
        active = is_task_active()
        if active is True:
            return True
        if active is False:
            saw_state = True

        if monotonic() >= deadline:
            return not saw_state

        await sleep(min(poll_interval, max(0.0, deadline - monotonic())))


def build_clean_command_not_started_data(
    *,
    entity_id: str | None,
    command: str,
    timeout: int,
    entry_id: str | None = None,
) -> dict[str, Any]:
    """Build the event payload for a clean command that never started."""
    data: dict[str, Any] = {
        "command": command,
        "timeout": timeout,
    }
    if entity_id:
        data["entity_id"] = entity_id
    if entry_id:
        data["entry_id"] = entry_id
    return data


def async_watch_clean_command_started(
    hass: Any,
    coordinator: Any,
    command: str,
    *,
    entity_id: str | None = None,
) -> None:
    """Watch a just-issued clean command and fire an event if it never starts.

    Runs as a background task so the calling service returns immediately.
    """
    timeout = resolve_clean_command_watch_timeout(coordinator.config_entry.options)
    if timeout <= 0:
        return

    async def _watch() -> None:
        started = await async_watch_until_task_starts(
            timeout=timeout,
            is_task_active=lambda: _task_active(coordinator),
        )
        if started:
            return
        hass.bus.async_fire(
            EVENT_CLEAN_COMMAND_NOT_STARTED,
            build_clean_command_not_started_data(
                entity_id=entity_id,
                command=command,
                timeout=timeout,
                entry_id=getattr(coordinator.config_entry, "entry_id", None),
            ),
        )

    coordinator.config_entry.async_create_background_task(
        hass,
        _watch(),
        name=f"{DOMAIN}_watch_clean_command",
    )


def _task_active(coordinator: Any) -> bool | None:
    """Read whether a task is active, or None when the state is unreadable."""
    from .v1_task_state import is_v1_task_active

    data = getattr(coordinator, "data", None)
    if data is None:
        return None
    status = getattr(data, "status", None)
    if status is None:
        return None
    return is_v1_task_active(
        state=status.state,
        in_cleaning=status.in_cleaning,
        in_returning=status.in_returning,
    )
