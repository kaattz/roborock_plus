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
import math
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
        #: Whether a return through the door is still in progress.
        #:
        #: The robot's own state cannot answer this. A return goes
        #: ``returning_home -> paused -> returning_home -> docking -> charging``, and
        #: ``paused`` is not a returning state, so a snapshot test says "not
        #: returning" for most of the trip. Measured 2026-11-08 16:16: the pause was
        #: confirmed, the door opened, and the departure rule -- seeing "outside" and
        #: no return -- shut the door again 1.4 seconds later, then repeated the whole
        #: cycle. The latch is what makes ``is_on_the_way_back`` true for the whole
        #: trip instead of only its first second.
        self.return_in_progress = False
        #: Set once the robot is confirmed on its dock again; only then is the return
        #: over. Cleared when a new return starts.
        self.return_completed = False
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

    def is_on_the_way_back(self) -> bool:
        """Whether the robot is heading in, or already home with work unfinished.

        Three shapes, one meaning: the door is the robot's way *in*, so closing it
        now would shut the robot out.

        * a return is latched in progress -- it stays latched across the pause this
          integration itself commands, which is the case a snapshot cannot see;
        * driving home -- ``returning_home`` / ``docking`` /
          ``going_to_wash_the_mop``;
        * back on the dock while the clean is still running -- charging, emptying
          the bin, washing the mop. ``task_active`` stays on for these, so the
          robot will drive out again by itself.

        Measured 2026-11-08 14:33:16: a returning robot reports
        ``outside_danger_zone = on`` for the whole trip through the doorway, so a
        rule that only asks "is it outside" shut the door on it mid-approach.

        Measured again at 16:16 the same day: the pause is commanded by this
        integration, so the robot reads ``paused`` -- not a returning state -- for
        the seconds between the pause and the resume. That gap is exactly when the
        door was reopened for it, and the departure rule closed it again. Hence the
        latch.
        """
        if self.return_in_progress and not self.return_completed:
            # The latch outranks the snapshot, but not reality: a robot confirmed on
            # its dock is home, so the return it was in cannot still be in progress.
            if self.robot_docked():
                self.note_return_completed()
            else:
                return True
        if self.is_returning():
            return True
        return bool(self.robot_docked() and self.task_running())

    def note_return_latched(self) -> None:
        """Mark that a return has started and must not be closed out."""
        self.return_in_progress = True
        self.return_completed = False

    def note_return_completed(self) -> None:
        """Mark that the robot is home again, so the door is no longer its way in."""
        self.return_completed = True

    def contained_position(self) -> tuple[float, float] | None:
        """Return the trusted map coordinate, or None when it cannot be used.

        Untrusted is a refusal, not a zero: a coordinate that did not pass the
        charger cross-check tells us nothing about where the robot stands.
        """
        if getattr(self.robot, 'fresh', False) is not True:
            return None
        if getattr(self.robot, 'position_trusted', False) is not True:
            return None
        x = getattr(self.robot, 'x', None)
        y = getattr(self.robot, 'y', None)
        if x is None or y is None:
            return None
        return float(x), float(y)

    def trip_finished(self) -> bool:
        """Whether the robot reports the waypoint trip is over.

        ``going_to_target`` is an active task in ``v1_task_state``, so this stays
        false for the whole drive *including* a mid-trip relocalisation, and turns
        true only when the robot announces it has stopped.
        """
        if self.task_running():
            return False
        return self.robot_state() == 'idle'

    def controlled_exit_arrived(self, target: tuple[float, float]) -> bool:
        """Whether the robot has finished its run to the waypoint.

        The trip ending is the answer, because that is what the robot's own
        command does: ``app_goto_target(x, y)`` drives to that point, and the
        robot reports the trip over when it gets there.

        Judging arrival by distance instead made the configured distance
        meaningless whenever the tolerance exceeded it. With a 100mm waypoint and
        a 200mm tolerance, a robot that had merely stepped outside the zone edge
        was 77mm from the waypoint -- inside the tolerance -- so it counted as
        arrived and the door shut before it had travelled the distance it was
        asked for. Keeping that branch "as a shortcut" reproduces the exact bug,
        so it is gone rather than reordered.

        ``robot_outside()`` is still the gate, and it is checked against the
        robot's **current** position: a robot inside the danger zone is never
        "arrived", whatever state it reports. That is the check that decides
        whether a door may be closed, and it cannot be satisfied by a waypoint.

        ``target`` is accepted for call-site compatibility and for
        ``controlled_exit_arrival_offset``; it does not decide arrival.
        """
        if not self.robot_outside():
            return False
        return self.trip_finished()

    def controlled_exit_arrival_offset(self, target: tuple[float, float]) -> float | None:
        """Return how far the robot actually stopped from the waypoint, in mm.

        ``None`` when the position cannot be trusted, which is a different
        statement from "zero".
        """
        position = self.contained_position()
        if position is None:
            return None
        return math.hypot(position[0] - target[0], position[1] - target[1])

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

        It always reports `outside` when it finishes. That is correct for the exit
        it was written for, and a *return* must not inherit it: `update_returning`
        overrides the phase straight after this returns, because `outside` is the
        value the departure rule closes the door on (measured 2026-11-08 14:33:16).
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
        if self.is_on_the_way_back():
            # The robot is driving in, or is home with work still to do. Whatever
            # its position says, this is not "the clean went out and the door can
            # be shut" -- that mistake closed the door on a returning robot at
            # 14:33:16 on 2026-11-08 and left it stuck outside.
            return False
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
            # A failure to release the robot is as serious as a failure to close:
            # the door being shut does not make a robot standing still a success.
            release_failed = self.phase == 'blocked'
        else:
            try:
                await self.close_door()
                closed = True
            except GarageControlError as err:
                self.last_error = err.reason
                self.phase = 'blocked'
                closed = False
            release_failed = False
        # The phase reflects what actually happened: reporting "closed_outside"
        # after a refused close would tell the next tick a lie and hide the fact
        # that the door is still open.
        if closed and not release_failed:
            self.task_in_progress = False
            self.phase = 'closed_outside'
        else:
            # Never overwrite a reason that explains the failure. `close_door()`
            # clears `last_error` on its success path, and the resume step may have
            # set a more serious one (`resume_failed` leaves the robot standing
            # still). Replacing either with a bare 'blocked' hides the cause.
            self.phase = 'blocked'
            if self.last_error is None:
                self.last_error = 'close_not_confirmed'
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

        The release runs even when the close fails, and a failure of the release
        itself is recorded instead of being swallowed. Measured 2026-11-08
        14:33:50: the robot was paused, the close did not go through, and no
        resume followed -- it stood still until someone noticed.

        Returns whether the door ended up closed.
        """
        self.phase = 'pausing_exit'
        await pause()
        if not await self._confirm_pause():
            # Not confirmed stopped: no door movement and no resume. See
            # `update_returning` for why releasing here is the wrong trade.
            self.phase = 'blocked'
            self.last_error = 'pause_not_observed'
            return False
        closed = False
        close_error: str | None = None
        try:
            await self.close_door()
            closed = True
        except GarageControlError as err:
            close_error = err.reason
            self.last_error = err.reason
            self.phase = 'blocked'
        finally:
            # Released even when closing failed: a robot left standing still
            # forever is a worse outcome than an open door. A failure here is the
            # more serious one, so it takes precedence in `last_error`.
            try:
                await resume()
            except Exception as err:  # noqa: BLE001 - reported, never swallowed
                reason = getattr(err, 'reason', None) or 'resume_failed'
                _LOGGER.error(
                    'Garage could not release the paused robot (%s) after an exit '
                    'attempt that %s; it is still paused',
                    reason,
                    'closed the door' if closed else f'failed to close ({close_error})',
                )
                self.last_error = reason
                self.phase = 'blocked'
        return closed

    #: How long a pause command gets to show up in the robot's own status.
    #:
    #: One status poll is not enough: the sample read straight after the command is
    #: usually the one taken *before* it. Measured 2026-11-08 15:19:49 -- the
    #: robot was paused, the integration read a stale `returning_home`, declared
    #: `pause_not_observed`, and stopped. Five seconds covers several polls at the
    #: one-second local cadence and is still far shorter than any door travel.
    PAUSE_CONFIRM_TIMEOUT_SECONDS = 5.0
    PAUSE_CONFIRM_POLL_SECONDS = 0.2

    #: How many times the pause confirmation may re-read the status before giving
    #: up, independent of the clock.
    #:
    #: The clock bound alone is not enough. `self.clock` is injected, and a caller
    #: (or a test double) that returns a constant would make the loop spin forever
    #: -- hanging the sequence while a robot sits paused. The iteration cap makes
    #: the loop terminate on its own terms, which is what a safety wait must do.
    PAUSE_CONFIRM_MAX_READS = 60

    async def _confirm_pause(self) -> bool:
        """Wait until the robot's own status says it is paused.

        Returns whether the pause was observed. The caller must not move the door
        on a ``False``, and must not resume the robot either: a robot whose stop
        cannot be seen may still be driving, and a resume would send it at a door
        that was never opened. Stopping is inconvenient; driving into a door is
        damage, and those are not exchangeable.

        Waiting at all is the fix for 2026-11-08 15:19:49, where a single lagging
        sample was read as "the pause failed" while the robot had in fact stopped.
        """
        deadline = self.clock() + self.PAUSE_CONFIRM_TIMEOUT_SECONDS
        for _ in range(self.PAUSE_CONFIRM_MAX_READS):
            self.refresh()
            if self.robot_state() == 'paused':
                return True
            if self.clock() >= deadline:
                return False
            await self.sleep(self.PAUSE_CONFIRM_POLL_SECONDS)
        return False

    async def update_returning(
        self,
        *,
        pause: Callable[[], Awaitable[Any]],
        resume: Callable[[], Awaitable[Any]],
    ) -> None:
        """Pause, open, resume -- in that order, each step observed.

        The order is not stylistic. The robot may be sitting close to the dock
        when it decides to go home, and the door swings toward the opening; a
        door told to move while the robot is still driving is a collision. So the
        stop is confirmed first, the door is opened and confirmed second, and only
        then is the robot released to drive through.

        Every step waits for its own evidence. Reading the status once and calling
        that "the pause failed" is how 2026-11-08 15:19:49 went wrong: the sample
        in hand was the one taken before the command, the robot was in fact
        stopped, and the sequence gave up with the door shut and the robot paused.
        """
        self.refresh()
        if not self.is_returning():
            return
        if self.open_confirmed():
            # Already open: nothing to do, but the return is still under way, so
            # latch it -- otherwise a later tick sees `paused` and shuts the door.
            self.note_return_latched()
            return
        self.note_return_latched()
        self.phase = 'pausing_return'
        await pause()
        if not await self._confirm_pause():
            # Not confirmed stopped. The door is not touched and the robot is NOT
            # resumed: an unconfirmed stop may still be a moving robot, and a
            # resume would send it at a door that never opened. It waits for a
            # human, which is inconvenient and safe.
            self.phase = 'blocked'
            self.last_error = 'pause_not_observed'
            raise GarageControlError('pause_not_observed')
        self.phase = 'opening'
        await self.open_door()
        self.phase = 'resuming_return'
        await resume()
        # Deliberately not 'outside'. That value means "a clean is running beyond
        # the door", and the departure rule closes on it -- which is how the door
        # was shut on a robot driving home at 14:33:16 on 2026-11-08. The return
        # stays in its own phase until the robot is docked.
        self.phase = 'resuming_return'

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

        if self.robot_docked():
            # The robot is home. Whatever happened before, it is no longer on its way
            # in, so the latch is released and the door is the clean's to close.
            self.note_return_completed()

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
