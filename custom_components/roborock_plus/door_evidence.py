"""Door endpoint facts and passage policy, independent of Home Assistant.

All timestamps are supplied by one monotonic clock. A successful service call
starts an estimate; it never proves physical arrival. HA wiring remains
responsible for identifying external commands and calling ``invalidate``.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any, Literal, NamedTuple


class EndpointEvidence(NamedTuple):
    """An independent endpoint; unknown is distinct from not reached."""

    reached: bool | None
    quality: Literal["confirmed", "estimated", "unknown"]
    source: str
    reason: str | None = None


class DoorState(NamedTuple):
    """One coherent observation and its policy result."""

    open: EndpointEvidence
    close: EndpointEvidence
    cover_available: bool
    direction: str | None = None
    operation_id: int | None = None
    allow_estimated: bool = False

    @property
    def open_confirmed(self) -> bool | None:
        """Expose estimated endpoints as unknown on physical fact entities."""
        return self.open.reached if self.open.quality == "confirmed" else None

    @property
    def closed_confirmed(self) -> bool | None:
        """Expose estimated endpoints as unknown on physical fact entities."""
        return self.close.reached if self.close.quality == "confirmed" else None

    @property
    def passage_allowed(self) -> bool | None:
        """A closing door cannot grant passage while its open switch releases."""
        if not self.cover_available:
            return None
        if self.direction == "close":
            return False
        if self.open.reached is None:
            return None
        if not self.open.reached:
            return False
        if self.open.quality == "confirmed":
            return True
        if self.open.quality == "estimated":
            return self.allow_estimated
        return None


class _Command(NamedTuple):
    direction: str
    started_at: float
    operation_id: int


def _number(value: Any, minimum: float, maximum: float) -> float | None:
    """Reject strings, booleans and non-finite safety parameters, without clamp."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not isfinite(number) or not minimum <= number <= maximum:
        return None
    return number


class DoorEvidence:
    """Track selected sources without falling back when a source fails.

    Configuration is copied so a running operation cannot silently change its
    meaning. A changed binding must create a new instance. ``invalidate`` clears
    every command/stability epoch; controller faults and manual holds are still
    controller policy, beyond these physical facts.
    """

    def __init__(self, binding: Mapping[str, Any]) -> None:
        self._binding = dict(binding)
        self._command: _Command | None = None
        self._stable_since: dict[str, float] = {}
        self._last_now: float | None = None

    def note_command(self, direction: str, now: float, operation_id: int) -> None:
        """Record an accepted full-travel command, starting a new operation."""
        valid_now = self._clock(now)
        if valid_now is None:
            return
        if direction not in ("open", "close"):
            self.invalidate("unsupported_door_command")
            return
        if not isinstance(operation_id, int) or isinstance(operation_id, bool):
            self.invalidate("invalid_operation_id")
            return
        self._stable_since.clear()
        self._command = _Command(direction, valid_now, operation_id)

    def invalidate(self, reason: str) -> None:
        """Revoke estimates and stable facts; the controller retains fault reasons."""
        self._command = None
        self._stable_since.clear()

    def observe(
        self,
        now: float,
        *,
        cover_available: bool,
        position: Any = None,
        open_contact: str | None = None,
        close_contact: str | None = None,
        restored: bool = False,
    ) -> DoorState:
        """Consume current input; callers must exclude restored provider values."""
        valid_now = self._clock(now)
        if valid_now is None:
            return self._unknown_state("invalid_clock", cover_available is True)
        if cover_available is not True:
            self.invalidate("feedback_unavailable")
            return self._unknown_state("feedback_unavailable", False)
        if restored:
            self.invalidate("restored_snapshot")
            return self._unknown_state("restored_snapshot", True)

        contacts = {"open": open_contact, "close": close_contact}
        if all(self._contact_reached(direction, contacts[direction]) is True for direction in ("open", "close")):
            self.invalidate("endpoint_conflict")
            return self._unknown_state("endpoint_conflict", True)

        # A lost selected source cancels its active estimate. Recovery cannot
        # resurrect a command whose motion was unobservable during that gap.
        if self._command is not None:
            direction = self._command.direction
            source = self._binding.get(f"{direction}_source")
            if (
                source == "position" and _number(position, 0, 100) is None
                or source == "contact" and self._contact_reached(direction, contacts[direction]) is None
            ):
                self.invalidate("feedback_unavailable")

        opened = self._endpoint("open", valid_now, position, open_contact)
        closed = self._endpoint("close", valid_now, position, close_contact)
        if opened.reached is True and closed.reached is True:
            # In particular, a timer may not overrule a still-active physical
            # limit switch. A stalled door can echo its target indefinitely.
            self.invalidate("endpoint_conflict")
            return self._unknown_state("endpoint_conflict", True)
        command = self._command
        return DoorState(
            opened,
            closed,
            True,
            command.direction if command is not None else None,
            command.operation_id if command is not None else None,
            self._binding.get("allow_estimated") is True,
        )

    def _clock(self, now: Any) -> float | None:
        valid_now = _number(now, 0, float("inf"))
        if valid_now is None or (self._last_now is not None and valid_now < self._last_now):
            self.invalidate("invalid_clock")
            # A new clock epoch can start at the next valid observation, but no
            # command or stable interval from the old epoch survives it.
            self._last_now = None
            return None
        self._last_now = valid_now
        return valid_now

    def _unknown(self, direction: str, reason: str) -> EndpointEvidence:
        self._stable_since.pop(direction, None)
        source = self._binding.get(f"{direction}_source")
        return EndpointEvidence(None, "unknown", source if isinstance(source, str) else "unconfigured", reason)

    def _unknown_state(self, reason: str, cover_available: bool) -> DoorState:
        return DoorState(self._unknown("open", reason), self._unknown("close", reason), cover_available)

    def _contact_reached(self, direction: str, value: Any) -> bool | None:
        if self._binding.get(f"{direction}_source") != "contact":
            return None
        entity_id = self._binding.get(f"{direction}_contact")
        polarity = self._binding.get(f"{direction}_contact_state")
        if not isinstance(entity_id, str) or not entity_id.startswith("binary_sensor."):
            return None
        if not isinstance(polarity, str) or polarity not in ("on", "off"):
            return None
        if not isinstance(value, str) or value not in ("on", "off"):
            return None
        return value == polarity

    def _settled(self, direction: str, reached: bool, now: float, seconds: float) -> bool:
        if not reached:
            self._stable_since.pop(direction, None)
            return False
        since = self._stable_since.setdefault(direction, now)
        return now - since >= seconds

    def _endpoint(self, direction: str, now: float, position: Any, contact: Any) -> EndpointEvidence:
        source = self._binding.get(f"{direction}_source")
        if source == "contact":
            reached = self._contact_reached(direction, contact)
            settle = _number(self._binding.get("contact_settle_seconds"), 0, 60)
            if settle is None or not self._binding.get(f"{direction}_contact") or self._binding.get(f"{direction}_contact_state") not in ("on", "off"):
                return self._unknown(direction, "invalid_configuration")
            if reached is None:
                return self._unknown(direction, "feedback_unavailable")
            if not reached:
                self._stable_since.pop(direction, None)
                return EndpointEvidence(False, "confirmed", "contact")
            if not self._settled(direction, reached, now, settle):
                return EndpointEvidence(None, "unknown", "contact", "endpoint_unsettled")
            return EndpointEvidence(True, "confirmed", "contact")

        if source == "position":
            kind = self._binding.get("position_kind")
            opened = _number(self._binding.get("open_position"), 0, 100)
            closed = _number(self._binding.get("close_position"), 0, 100)
            settle = _number(self._binding.get("position_settle_seconds"), 0, 60)
            if kind not in ("measured", "estimated") or opened is None or closed is None or opened <= closed or settle is None:
                return self._unknown(direction, "invalid_configuration")
            value = _number(position, 0, 100)
            if value is None:
                return self._unknown(direction, "feedback_unavailable")
            reached = value >= opened if direction == "open" else value <= closed
            settled = self._settled(direction, reached, now, settle)
            if kind == "estimated":
                estimate = self._estimate(direction, now)
                if estimate is not None:
                    return estimate
                if not reached:
                    self._stable_since.pop(direction, None)
                    return EndpointEvidence(False, "estimated", "position", "unverified_position")
            elif not reached:
                self._stable_since.pop(direction, None)
                return EndpointEvidence(False, "confirmed", "position")
            if not settled:
                return EndpointEvidence(None, "unknown", "position", "endpoint_unsettled")
            quality = "confirmed" if kind == "measured" else "estimated"
            return EndpointEvidence(True, quality, "position", None if kind == "measured" else "unverified_position")

        if source == "timed":
            estimate = self._estimate(direction, now)
            if estimate is not None:
                return estimate
            return EndpointEvidence(True, "estimated", "timed", "unverified_position")
        return self._unknown(direction, "invalid_configuration")

    def _estimate(self, direction: str, now: float) -> EndpointEvidence | None:
        """Return a rejection, or None once this direction's full travel elapsed."""
        travel = _number(self._binding.get(f"{direction}_travel_seconds"), 1, 600)
        margin = _number(self._binding.get("travel_margin_seconds"), 0, 120)
        if travel is None or margin is None:
            return self._unknown(direction, "invalid_configuration")
        command = self._command
        if command is None or command.direction != direction:
            # Current source failures were checked before estimating. A revoked
            # old command is not evidence that the source is still unavailable.
            return self._unknown(direction, "command_required")
        if now - command.started_at < travel + margin:
            return EndpointEvidence(None, "unknown", str(self._binding[f"{direction}_source"]), "travel_in_progress")
        return None
