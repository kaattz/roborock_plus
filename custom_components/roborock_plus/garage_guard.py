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


class DoorEchoWindow:
    """Remembers when a cover command was last seen, to distrust its echo.

    The pre-command check used to trust any position it read, on the grounds that
    "no command has been issued *by this call*". That reasoning is about the call,
    not about the device: another automation may have commanded the same door a
    moment earlier, and the device writes the commanded target into
    `current-position` immediately while the door is still closed.

    Measured path: the state machine's phase C issues `open_cover`, the device
    echoes 100 while the door is physically at 5%, and a clean command arrives
    within the travel window -- the guard read 100, returned in 0.003s without
    calling a single service, and released the robot at a nearly-closed door.

    So a reading only counts when the door's *own* last change is older than one
    travel. That is the interval the device needs to finish moving, so a position
    that has held for it cannot be an echo of a fresh command.

    Kept as a plain object with an injected clock so the rule is testable without
    Home Assistant. The caller holds one per config entry.
    """

    #: How long a reading must have been stable to be believed when a command was
    #: seen recently. Matches the post-command gate: one full travel.
    SETTLE = DEFAULT_GARAGE_DOOR_MIN_TRAVEL

    def __init__(self) -> None:
        self._last_command_at: float | None = None

    def note_command(self, now: float) -> None:
        """Record that a cover command was issued for this door."""
        self._last_command_at = now

    def trusts(
        self,
        now: float,
        position: Any,
        changed_at: float | None,
    ) -> bool:
        """Return whether a pre-command reading may be acted on.

        ``changed_at`` is when the cover entity was last updated (HA's
        ``last_updated_timestamp``). It advances only on a real change -- core
        returns early when state and attributes are identical -- so a polling
        integration re-reporting the same position does not refresh it.

        ``None`` is treated as unsafe rather than as "long ago": the one thing
        this must never do is release a robot at a door that is still moving
        because a timestamp could not be read.
        """
        if self._last_command_at is None:
            # No command seen since start-up, so this reading is not a known echo.
            return is_garage_door_open_enough(position)

        if now - self._last_command_at >= self.SETTLE:
            # The device has had time to finish travelling, so whatever position
            # it reports now is real rather than that command's echo.
            return is_garage_door_open_enough(position)

        # A command was seen within the travel window. The position alone cannot
        # be trusted, so require the reading to have been stable for a full travel
        # too -- meaning it predates the command rather than echoing it.
        if changed_at is None:
            return False
        return now - changed_at >= self.SETTLE and is_garage_door_open_enough(
            position
        )


async def async_guard_garage_open(
    hass: Any,
    options: Mapping[str, Any],
    echo_window: DoorEchoWindow | None = None,
) -> None:
    """Open the configured garage door and wait until it is fully open.

    ``echo_window`` carries "when was this door last commanded" across calls. It
    is what makes the pre-command check safe: without it, a position read here is
    trusted on the reasoning that *this* call issued no command -- which says
    nothing about a command another automation issued a moment ago.
    """
    from homeassistant.exceptions import HomeAssistantError

    if not is_garage_guard_ready(options):
        return

    cover_entity_id = str(options[CONF_GARAGE_DOOR_ENTITY_ID])
    window = echo_window if echo_window is not None else DoorEchoWindow()

    # Already open, and stably so? Nothing to do, and no wait to impose. The
    # stability requirement is what rejects a position that changed because of a
    # command this call did not issue.
    if window.trusts(
        time.monotonic(),
        _configured_cover_position(hass, cover_entity_id),
        _configured_cover_changed_at(hass, cover_entity_id),
    ):
        return

    window.note_command(time.monotonic())
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


def _configured_cover_changed_at(hass: Any, cover_entity_id: str) -> float | None:
    """Return when the cover entity last actually changed, as epoch seconds.

    Uses ``last_updated_timestamp`` rather than ``last_changed``: core advances
    ``last_updated`` on any real change including an attribute-only one, and
    ``current_position`` is an attribute. It returns early when state and
    attributes are both identical, so an integration re-reporting the same
    position does not refresh this -- which is what makes it usable as evidence
    that a reading has settled.

    ``None`` when the timestamp is unavailable; callers must treat that as
    unsafe rather than as "long ago".
    """
    state = hass.states.get(cover_entity_id)
    if state is None:
        return None
    stamp = getattr(state, "last_updated_timestamp", None)
    if stamp is None:
        return None
    try:
        return float(stamp)
    except (TypeError, ValueError):
        return None
