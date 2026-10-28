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
        #: Whether a clean task started by this integration is in progress. The door
        #: is shared with the household, so the integration has permission to move it
        #: only while it is running a task: outside that scope the door is not the
        #: integration's to touch at all.
        self.task_in_progress = False
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
        """Whether a clean/dock task is still in progress.

        Uses the robot's own ``task_active`` reading rather than the bare state
        name, because a mid-task visit to the dock reports ``charging`` -- which
        looks exactly like "finished" if you only read the state. ``task_active``
        consults ``in_cleaning``/``in_returning`` first, so a robot charging in the
        middle of a job still counts as busy (see ``v1_task_state``).

        This matters physically: a robot that went back to recharge will drive out
        again by itself. Shutting the door on it because "it is docked" would put a
        closed door in its path minutes later.

        Unknown is treated as still running: refusing to close is recoverable,
        closing on a live task is not.
        """
        active = getattr(self.robot, 'task_active', None)
        if active is None:
            state = self.robot_state()
            if state in ('', 'unknown', 'unavailable'):
                return True
            return state not in PARKED_STATES
        return bool(active)

    # -- door actions --------------------------------------------------------

    async def open_door(self) -> None:
        """Open and confirm, or stand down without latching.

        Called *before* a clean command. The confirmation requirement is what
        makes "proactive" safe: the robot is not released until the door is known
        to be open.

        Everything here happens inside a task this integration is starting, so it is
        the moment the door becomes ours to move. The flag is set even when the door
        is already open: the household opening it first does not take the task away.
        """
        self.refresh()
        reason = self.door_blocked_reason()
        if reason is not None:
            self.phase = 'blocked'
            self.last_error = reason
            raise GarageControlError(reason)
        if self.open_confirmed():
            self.task_in_progress = True
            self.phase = 'outside'
            self.last_error = None
            return
        self.phase = 'opening'
        try:
            self.evidence.invalidate('opening')
            await self.move_door('open')
            await self._wait_for('open')
            self.task_in_progress = True
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

    async def update_after_departure(
        self,
        *,
        pause: Callable[[], Awaitable[Any]] | None = None,
        resume: Callable[[], Awaitable[Any]] | None = None,
    ) -> bool:
        """Close once the robot is confirmed outside and the door is open.

        The robot is paused first. Measured 2026-10-27: it often cleans right next
        to the dock before coming back to charge, so "outside the danger zone" and
        "far from the door" are not the same thing. A door swinging shut behind a
        robot that is still a few centimetres away can hit it, and a pause costs
        nothing here -- the robot simply waits, then carries on.

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
        if pause is not None and resume is not None:
            closed = await self._pause_open_close_resume(pause, resume)
        else:
            try:
                await self.close_door()
                closed = True
            except GarageControlError as err:
                self.last_error = err.reason
                self.phase = 'blocked'
                closed = False
        # The phase reflects what actually happened: reporting "closed_outside"
        # after a refused close would tell the next tick a lie and hide the fact
        # that the door is still open.
        if closed:
            self.task_in_progress = False
        self.phase = 'closed_outside' if closed else 'blocked'
        return True

    async def _pause_open_close_resume(
        self,
        pause: Callable[[], Awaitable[Any]],
        resume: Callable[[], Awaitable[Any]],
    ) -> bool:
        """Hold the robot still for the moment the door is moving.

        The pause must be observed before the door is told to move: a door
        commanded while the robot is still driving is exactly the collision this
        is meant to prevent. If the pause cannot be confirmed the door is left
        open -- an open door is merely untidy, a door closing on a moving robot is
        damage.

        Returns whether the door ended up closed.
        """
        self.phase = 'pausing_exit'
        await pause()
        self.refresh()
        if self.robot_state() != 'paused':
            self.phase = 'blocked'
            self.last_error = 'pause_not_observed'
            return False
        try:
            await self.close_door()
            return True
        except GarageControlError as err:
            self.last_error = err.reason
            self.phase = 'blocked'
            return False
        finally:
            # Released even when closing failed: a robot left standing still
            # forever is a worse outcome than an open door.
            await resume()

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

    async def sequence_finished(self) -> bool:
        """Advance one running clean. Returns whether the sequence is over.

        The rules, in order. Only the states that matter are listed; anything else does
        nothing at all.

        1. A task is running and the robot is on its dock while the door is *not* open:
           open it. The door is the robot's only way out, and it leaves again on its own
           after charging, washing or emptying. A closed door in its path is a collision.
           This also covers a household member closing the door mid-task: the clean is
           still running, so the door is reopened.
        2. A task is running but the robot is elsewhere: do nothing. It is working, and
           closing behind it belongs to ``update_after_departure``.
        3. No task, robot docked, door open: the clean is over; close the door.
        4. No task, robot docked, door closed: already finished.
        5. No task, robot away from the dock: not over yet; it is still driving home.
        """
        # Read reality before deciding. Every other entry point refreshes first; without
        # it this answered from whatever the last caller happened to leave in the
        # snapshot, which is how a robot reported as cleaning was once mistaken for one
        # that had finished.
        self.refresh()

        if not self.task_in_progress:
            # No clean of ours is running, so this table does not apply at all: the door
            # belongs to the household, whatever position it is in.
            return False

        if self.task_running():
            if self.robot_docked() and not self.open_confirmed():
                # Rule 1: the robot is home mid-task and must be able to get out again.
                if self.door_blocked_reason() is not None:
                    self.phase = 'blocked'
                    return False
                try:
                    await self.open_door()
                except GarageControlError as err:
                    self.last_error = err.reason
                    self.phase = 'blocked'
                return False
            # Rule 2: working elsewhere; the departure rule owns the door.
            return False

        if not self.robot_docked():
            # Rule 5: still on its way home.
            return False

        if self.closed_confirmed():
            # Rule 4: finished, and the door is already shut.
            self.task_in_progress = False
            self.phase = 'idle'
            return True
        if self.door_blocked_reason() is not None:
            # Cannot read the door. Do not act on a guess; the sequence ends anyway so
            # a stuck sensor cannot keep a caller blocked for hours.
            self.phase = 'blocked'
            self.task_in_progress = False
            return True
        # Rule 3: the clean is over and the door it used is still open.
        try:
            await self.close_door()
        except GarageControlError as err:
            self.last_error = err.reason
            self.phase = 'blocked'
            self.task_in_progress = False
            return True
        self.task_in_progress = False
        self.phase = 'idle'
        return True
