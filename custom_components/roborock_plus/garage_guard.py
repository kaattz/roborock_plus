"""Garage-door guard for HA-initiated Roborock clean commands."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

DOMAIN = "roborock_plus"

CONF_GARAGE_GUARD_ENABLED = "garage_guard_enabled"
CONF_GARAGE_DOOR_ENTITY_ID = "garage_door_entity_id"

DEFAULT_GARAGE_DOOR_OPEN_POSITION = 95

# A position read sooner than one full travel is the device echoing the command
# back, not the door. The guard waits this long before it believes a reading.
# Kept in step with door_timing.ECHO_SETTLE_SECONDS by
# tests/test_garage_guard.py::test_min_travel_outlasts_the_echo_window.
DEFAULT_GARAGE_DOOR_MIN_TRAVEL = 45

# Min travel plus a bounded wait for a door that is genuinely slow. A timeout
# shorter than the settle would fail a door that is opening perfectly well.
DEFAULT_GARAGE_DOOR_TIMEOUT = DEFAULT_GARAGE_DOOR_MIN_TRAVEL + 45

_GUARDED_CLEAN_COMMANDS = {
    "APP_GOTO_TARGET",
    "APP_START",
    "APP_SEGMENT_CLEAN",
    "APP_SPOT",
    "APP_ZONED_CLEAN",
    "app_goto_target",
    "app_start",
    "app_segment_clean",
    "app_spot",
    "app_zoned_clean",
}


def command_value(command: Any) -> str:
    """Return the Roborock wire command value."""
    if hasattr(command, "value"):
        return str(command.value)
    return command


def should_guard_clean_command(command: Any) -> bool:
    """Return whether a command may move the robot out of the dock."""
    return command_value(command) in _GUARDED_CLEAN_COMMANDS


def is_garage_door_open_enough(position: Any) -> bool:
    """Return whether the cover position is high enough to start moving."""
    if position is None:
        return False
    return float(position) >= DEFAULT_GARAGE_DOOR_OPEN_POSITION


def door_reached_open_position(elapsed: float, position: Any) -> bool:
    """Return whether enough time has passed *and* the position is high enough.

    Both halves are load-bearing. Position alone is the echo: the device reports
    the target the moment it is commanded. Time alone would accept a door that a
    physical obstruction stopped part-way.
    """
    if elapsed < DEFAULT_GARAGE_DOOR_MIN_TRAVEL:
        return False
    return is_garage_door_open_enough(position)


def is_garage_guard_ready(options: Mapping[str, Any]) -> bool:
    """Return whether the guard has enough config to run."""
    return bool(
        options.get(CONF_GARAGE_GUARD_ENABLED)
        and options.get(CONF_GARAGE_DOOR_ENTITY_ID)
    )


async def async_guard_garage_open(
    hass: Any,
    options: Mapping[str, Any],
) -> None:
    """Open the configured garage door and wait until it is fully open."""
    from homeassistant.exceptions import HomeAssistantError

    if not is_garage_guard_ready(options):
        return

    cover_entity_id = str(options[CONF_GARAGE_DOOR_ENTITY_ID])
    # No command has been issued yet, so this reading is not an echo and can be
    # trusted on its own. Waiting here would stall every start behind 45s even
    # when the door is already open.
    if is_garage_door_open_enough(
        _configured_cover_position(hass, cover_entity_id)
    ):
        return

    await hass.services.async_call(
        "cover",
        "open_cover",
        {"entity_id": cover_entity_id},
        blocking=True,
    )

    # The command was issued now; everything the device reports until the door
    # has physically travelled is the echo of this call.
    started = time.monotonic()
    try:
        async with asyncio.timeout(DEFAULT_GARAGE_DOOR_TIMEOUT):
            while not door_reached_open_position(
                time.monotonic() - started,
                _configured_cover_position(hass, cover_entity_id),
            ):
                await asyncio.sleep(0.5)
    except TimeoutError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="garage_guard_open_timeout",
            translation_placeholders={"entity_id": cover_entity_id},
        ) from err


def _configured_cover_position(hass: Any, cover_entity_id: str) -> Any:
    """Return the configured cover's raw position attribute.

    Deliberately does not decide whether the door is "open enough": the
    pre-command check and the post-command wait need different judgements, and
    the post-command one additionally needs elapsed time.
    """
    from homeassistant.exceptions import HomeAssistantError

    state = hass.states.get(cover_entity_id)
    if state is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="garage_guard_cover_not_found",
            translation_placeholders={"entity_id": cover_entity_id},
        )
    return state.attributes.get("current_position")
