"""Garage-door guard for HA-initiated Roborock clean commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

DOMAIN = "roborock_plus"

CONF_GARAGE_GUARD_ENABLED = "garage_guard_enabled"
CONF_GARAGE_DOOR_ENTITY_ID = "garage_door_entity_id"

DEFAULT_GARAGE_DOOR_OPEN_POSITION = 95
DEFAULT_GARAGE_DOOR_TIMEOUT = 60

_GUARDED_CLEAN_COMMANDS = {
    "APP_START",
    "APP_SEGMENT_CLEAN",
    "APP_ZONED_CLEAN",
    "app_start",
    "app_segment_clean",
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
    if _is_configured_cover_open(hass, cover_entity_id):
        return

    await hass.services.async_call(
        "cover",
        "open_cover",
        {"entity_id": cover_entity_id},
        blocking=True,
    )

    try:
        async with asyncio.timeout(DEFAULT_GARAGE_DOOR_TIMEOUT):
            while not _is_configured_cover_open(hass, cover_entity_id):
                await asyncio.sleep(0.5)
    except TimeoutError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="garage_guard_open_timeout",
            translation_placeholders={"entity_id": cover_entity_id},
        ) from err


def _is_configured_cover_open(hass: Any, cover_entity_id: str) -> bool:
    from homeassistant.exceptions import HomeAssistantError

    state = hass.states.get(cover_entity_id)
    if state is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="garage_guard_cover_not_found",
            translation_placeholders={"entity_id": cover_entity_id},
        )
    return is_garage_door_open_enough(state.attributes.get("current_position"))
