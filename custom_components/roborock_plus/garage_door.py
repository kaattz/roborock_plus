"""Garage door control driven by where the robot is, not by which task it is.

Why this exists
---------------
The previous design tried to prove "this is the task I authorised" before acting:
task ids, clean-counter baselines, correlation flags, cancel requests. That
answers a question this household never asks -- every clean command enters
through Home Assistant, so a task is authorised by construction. The machinery
only produced latches: three times the controller refused to work on an idle,
docked robot until someone pressed reset, and reset itself was blocked by the
same record.

What it does instead
--------------------
Two rules, both about physical facts:

* opening is **proactive** -- the door is opened and confirmed before a clean
  command is sent, because waiting to observe ``cleaning`` means the robot has
  already moved. Measured 2026-10-23: pausing it mid-manoeuvre in a narrow
  opening leaves it spinning in place, never getting out.
* closing is **reactive** -- closed only after the robot is confirmed past the
  danger zone, or after the task ends with the robot docked.

Nothing latches. When the door feedback is unusable the controller declines to
act and waits; the next good sample resumes normal operation with no operator
step in between. There is deliberately no reset entry, because there is nothing
to release.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .door_evidence import DoorEvidence, DoorState

_LOGGER = logging.getLogger(__name__)

#: Robot states in which it is driving home and needs the door open.
RETURNING_STATES = frozenset({'returning_home', 'docking', 'going_to_wash_the_mop'})

#: Robot states in which it is provably not moving and not cleaning.
PARKED_STATES = frozenset({'charging', 'charging_complete', 'idle'})


class GarageControlError(ValueError):
    """A refusal the caller can act on, with a stable reason code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class GarageController:
    """One door, one robot's position and state, and a small phase machine.

    Injected dependencies keep this testable without Home Assistant:

    ``door_observation()``   -> dict: cover_available, position, open_contact,
                                close_contact, restored
    ``robot_observation()``  -> object: state, docked, outside_danger_zone,
                                position_trusted, fresh
    ``move_door(direction)`` -> awaitable that commands the cover
    ``close_allowed()``      -> family permission for closing
    """

    def __init__(
        self,
        *,
        binding: Mapping[str, Any],
        door_observation: Callable[[], dict[str, Any]],
        robot_observation: Callable[[], Any],
        move_door: Callable[[str], Awaitable[Any]],
        close_allowed: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.binding = dict(binding)
        self.door_observation = door_observation
        self.robot_observation = robot_observation
        self.move_door = move_door
        self.close_allowed = close_allowed
        self.clock = clock
        self.sleep = sleep

        self.evidence = DoorEvidence(self.binding)
        self.phase = 'idle'
        self.last_error: str | None = None
        self.robot = robot_observation()
        self.state: DoorState = self._observe_door()

    # -- observation ---------------------------------------------------------

    def _observe_door(self) -> DoorState:
        observed = self.door_observation()
        return self.evidence.observe(
            self.clock(),
            cover_available=observed.get('cover_available') is True,
            position=observed.get('position'),
            open_contact=observed.get('open_contact'),
            close_contact=observed.get('close_contact'),
            restored=observed.get('restored') is True,
        )

    def refresh(self) -> None:
        """Re-read both sides. Never commands anything."""
        self.robot = self.robot_observation()
        self.state = self._observe_door()

    @property
    def passage_allowed(self) -> bool | None:
        """Whether a person or robot could pass right now.

        Kept for the diagnostic entity: it is a rendering of the door facts, not
        an input to any decision here.
        """
        if not self.state.cover_available:
            return None
        if self.closed_confirmed():
            return False
        return True if self.open_confirmed() else None

    def door_blocked_reason(self) -> str | None:
        """Return why the door cannot be acted on right now, if it cannot."""
        if not self.state.cover_available:
            return 'feedback_unavailable'
        reasons = {self.state.open.reason, self.state.close.reason}
        for reason in ('endpoint_conflict', 'feedback_unavailable',
                       'restored_snapshot', 'invalid_configuration', 'invalid_clock'):
            if reason in reasons:
                return reason
        return None

    def open_confirmed(self) -> bool:
        """Return whether the door is known to be fully open."""
        return self.state.open_confirmed is True

    def closed_confirmed(self) -> bool:
        """Return whether the door is known to be fully closed."""
        return self.state.closed_confirmed is True

    def robot_outside(self) -> bool:
        """Return whether the robot is confirmed past the danger zone."""
        return bool(
            getattr(self.robot, 'fresh', False)
            and getattr(self.robot, 'position_trusted', False) is True
            and getattr(self.robot, 'outside_danger_zone', None) is True
            and not getattr(self.robot, 'docked', False)
        )

    def robot_docked(self) -> bool:
        return bool(getattr(self.robot, 'docked', False))

    def robot_state(self) -> str:
        state = getattr(self.robot, 'state', '')
        return str(getattr(state, 'name', state))

    def is_returning(self) -> bool:
        return self.robot_state() in RETURNING_STATES

    def task_running(self) -> bool:
        """Whether anything needs the doorway right now.

        Derived from the robot's own state rather than a stored task record: the
        question is physical, and the robot already answers it.
        """
        state = self.robot_state()
        if state in PARKED_STATES:
            return False
        if state in ('', 'unknown', 'unavailable'):
            return False
        return True

    # -- door actions --------------------------------------------------------

    async def open_door(self) -> None:
        """Open and confirm, or stand down without latching.

        Called *before* a clean command. The confirmation requirement is what
        makes "proactive" safe: the robot is not released until the door is known
        to be open.
        """
        self.refresh()
        reason = self.door_blocked_reason()
        if reason is not None:
            self.phase = 'blocked'
            self.last_error = reason
            raise GarageControlError(reason)
        if self.open_confirmed():
            self.phase = 'outside'
            self.last_error = None
            return
        self.phase = 'opening'
        try:
            self.evidence.invalidate('opening')
            await self.move_door('open')
            await self._wait_for('open')
            self.phase = 'outside'
            self.last_error = None
        except GarageControlError:
            raise
        except Exception as err:  # noqa: BLE001 - reported, never latched
            self.phase = 'blocked'
            self.last_error = getattr(err, 'reason', None) or 'door_command_failed'
            raise GarageControlError(self.last_error) from err

    async def close_door(self) -> None:
        """Close, but only when the evidence and the household allow it."""
        self.refresh()
        reason = self.door_blocked_reason()
        if reason is not None:
            self.phase = 'blocked'
            self.last_error = reason
            raise GarageControlError(reason)
        if not self.close_allowed():
            raise GarageControlError('close_not_allowed')
        if self.closed_confirmed():
            self.phase = 'idle'
            self.last_error = None
            return
        self.phase = 'closing'
        try:
            self.evidence.invalidate('closing')
            await self.move_door('close')
            await self._wait_for('close')
            self.last_error = None
        except GarageControlError:
            raise
        except Exception as err:  # noqa: BLE001
            self.phase = 'blocked'
            self.last_error = getattr(err, 'reason', None) or 'door_command_failed'
            raise GarageControlError(self.last_error) from err

    async def _wait_for(self, direction: str) -> None:
        """Wait until the endpoint is confirmed, bounded by the operation timeout."""
        timeout = float(self.binding.get('operation_timeout_seconds', 90))
        deadline = self.clock() + timeout
        confirm = self.open_confirmed if direction == 'open' else self.closed_confirmed
        while True:
            self.refresh()
            if confirm():
                return
            if self.clock() >= deadline:
                raise GarageControlError(f'{direction}_not_confirmed')
            await self.sleep(0.2)

    # -- the reactive rules --------------------------------------------------

    async def update_after_departure(self) -> bool:
        """Close once the robot is confirmed outside and the door is open.

        Returns whether anything was decided, so the caller can stop processing.
        Only ever closes, never opens: a slow sample can therefore not leave a
        robot facing a shut door.
        """
        self.refresh()
        if not self.robot_outside():
            return False
        if self.closed_confirmed():
            self.phase = 'closed_outside'
            return True
        if self.door_blocked_reason() is not None:
            self.phase = 'blocked'
            return True
        await self.close_door()
        self.phase = 'closed_outside'
        return True

    async def update_returning(
        self,
        *,
        pause: Callable[[], Awaitable[Any]],
        resume: Callable[[], Awaitable[Any]],
    ) -> None:
        """Pause, open, resume -- in that order, each step observed.

        Pausing is safe in a way that opening late is not: the robot stops and
        waits where it is, so the door has time. Resuming without a confirmed
        pause would drive the robot at a door that may still be moving.
        """
        self.refresh()
        if not self.is_returning():
            return
        if self.open_confirmed():
            return
        self.phase = 'pausing_return'
        await pause()
        self.refresh()
        if self.robot_state() != 'paused':
            self.phase = 'blocked'
            self.last_error = 'pause_not_observed'
            raise GarageControlError('pause_not_observed')
        self.phase = 'opening'
        await self.open_door()
        self.phase = 'resuming_return'
        await resume()
        self.phase = 'outside'

    async def update_idle(self) -> None:
        """Close when nothing is running and the robot sits on its dock."""
        self.refresh()
        if self.task_running():
            return
        if not self.robot_docked():
            return
        if self.closed_confirmed():
            self.phase = 'idle'
            return
        if self.door_blocked_reason() is not None:
            self.phase = 'blocked'
            return
        await self.close_door()
        self.phase = 'idle'
