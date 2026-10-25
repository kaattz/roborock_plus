"""Single-owner garage workflow. All device I/O is supplied by the HA adapter."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

_LOGGER = logging.getLogger(__name__)

_PREPARING_STATES = frozenset({'starting','washing_the_mop','washing_the_mop_2','attaching_the_mop','detaching_the_mop'})
_RESUMED_STATES = frozenset({'cleaning','spot_cleaning','zoned_cleaning','segment_cleaning',
                            'going_to_target','docking','returning_home','going_to_wash_the_mop'})
_CLEANING_STATES = frozenset({'cleaning', 'spot_cleaning', 'zoned_cleaning', 'segment_cleaning'})
_RETURN_STATES = frozenset({'docking', 'returning_home', 'going_to_wash_the_mop'})
# Phases with no workflow of our own in flight. An external door command here has
# nothing to collide with; anywhere else it might be about to fight a task or a
# door already travelling.
_NO_CONFLICT_PHASES = frozenset({'idle', 'outside_closed', 'waiting_close_permission'})
# The same rejections _preflight() refuses to move on. "No conflict" is only
# meaningful when the evidence itself is trustworthy.
_BLOCKING_ENDPOINT_REASONS = frozenset({
    'endpoint_conflict', 'feedback_unavailable', 'restored_snapshot',
    'invalid_clock', 'invalid_configuration',
})


class _ReturnPreempted(Exception):
    """A fresh return observation supersedes an in-task closing operation."""


class _DepartureCloseAborted(Exception):
    """Our close was reversed safely; a later real departure may close again."""


class GarageControlError(ValueError):
    """A rejected operation, with a stable diagnostic reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class GarageController:
    """Coordinate door movement without replaying robot commands or owning policies."""

    def __init__(
        self, binding: Mapping[str, Any], *, evidence: Any, session: Any,
        move_door: Callable[[str, int], Awaitable[None]],
        door_observation: Callable[[], dict[str, Any]],
        robot_observation: Callable[[], Any], close_allowed: Callable[[], bool],
        persist: Callable[[], Awaitable[None]], notify: Callable[[str | None], None],
        supports_stop: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        start_timeout: float = 60.0,
        send_robot: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self.binding = binding
        self.evidence = evidence
        self.session = session
        self.move_door = move_door
        self.door_observation = door_observation
        self.robot_observation = robot_observation
        self.close_allowed = close_allowed
        self.persist = persist
        self.notify = notify
        self.supports_stop = supports_stop
        self.clock = clock
        self.sleep = sleep
        self.start_timeout = start_timeout
        self.send_robot = send_robot
        self.phase = 'recovering'
        self.reason: str | None = None
        self.previous_phase: str | None = None
        self.operation_id = 0
        self._controlled_exit_target: tuple[int, int] | None = None
        self._controlled_exit_deadline: float | None = None
        self.state: Any = None
        self.robot: Any = None
        self._pending = False
        self._pending_operation: int | None = None
        self._operations: set[int] = set()
        self._closed = False
        self._start_deadline: float | None = None
        self._expect_resume = False
        self._resume_sample: Any = None
        self._departure_armed = False
        self._exit_due = False
        self._position_sample: Any = None
        self._closing_for_exit = False
        self._closing_session_id: str | None = None
        self._pause_session: str | None = None
        self._user_hold = False
        self._observation_deadline: float | None = None
        self._observation_session_id: str | None = None
        self._return_brake_attempted = False

    @property
    def passage_allowed(self) -> bool | None:
        if self.reason or self._closed:
            return False
        return None if self.state is None else self.state.passage_allowed

    def refresh(self) -> None:
        """Read current observations; never issue device commands here."""
        self.state = self.evidence.observe(self.clock(), **self.door_observation())
        self.robot = self.robot_observation()
        self.session.observe(self.robot)
        if self.binding.get('task_door_mode') == 'after_exit':
            self._observe_departure()

    def _observe_departure(self) -> None:
        """Arm on 'inside', fire on a later 'outside', and leave 'no sample' alone.

        ``outside_danger_zone`` is ``None`` whenever this poll failed to read a
        position -- measured 2026-10-25 at roughly once every 30s for about 1.2s,
        while the local MQTT connection was answering map requests twice a second.
        Treating that as "untrusted, drop everything" threw away the armed flag
        before the next good sample could report the crossing, so
        ``_close_after_departure()`` never ran and the door stayed open until it
        was closed by hand.

        "I did not read a position this time" and "this position cannot be
        trusted" are different facts and must not share a branch:

        * untrusted (``position_trusted`` false / stale) -- drop the arming. We do
          not know where the robot is, so we must not later decide the doorway is
          clear.
        * no sample (``outside is None``) -- say nothing. The previous decision
          stands; the next usable sample decides.
        """
        token = getattr(self.robot, 'position_sample_id', None)
        outside = getattr(self.robot, 'outside_danger_zone', None)
        if not (self.robot.fresh and self.robot.position_trusted):
            self._departure_armed = False
            self._exit_due = False
            return
        if outside is None:
            # Nothing was sampled. Neither advance nor rewind: an unread position
            # is not evidence that the robot went back inside, and inventing
            # "inside" would let a lost packet close the door on a robot that is
            # still in the cabinet.
            return
        if token is None or token == self._position_sample:
            return
        self._position_sample = token
        if outside is False:
            # Explicitly inside: this is the only thing that arms a departure.
            self._departure_armed = True
            self._exit_due = False
        elif self._departure_armed:
            self._exit_due = True
            self._departure_armed = False
        elif outside is True:
            # A sample we never armed for (first reading after a restart, or a
            # jump straight to "outside"). There was no observed departure, so
            # there is nothing to close for.
            self._exit_due = False

    def fault(self, reason: str, *, manual: bool = False) -> None:
        """Invalidate waiting operations; recovery of a sensor is not a reset."""
        previous_phase = self.phase
        self.previous_phase = previous_phase
        self.operation_id += 1
        self._pause_session = None
        self._observation_deadline = self._observation_session_id = None
        self.evidence.invalidate(reason)
        changed = self.reason != reason
        self.reason = reason
        self.phase = 'manual_hold' if manual else 'blocked'
        if changed:
            robot = self.robot
            utc, sampled = getattr(robot, 'utc', None), getattr(robot, 'sample_utc', None)
            age = utc - sampled if isinstance(utc, (int, float)) and isinstance(sampled, (int, float)) else None
            _LOGGER.warning(
                'Garage control %s: reason=%s previous_phase=%s phase=%s operation=%s '
                'state=%s fresh=%s trusted=%s outside=%s status_sample_age=%s '
                'map_sample=%s session=%s task=%s',
                'held for manual handling' if manual else 'blocked',
                reason, previous_phase, self.phase, self.operation_id,
                getattr(robot, 'state', None), getattr(robot, 'fresh', None),
                getattr(robot, 'position_trusted', None), getattr(robot, 'outside_danger_zone', None),
                age, getattr(robot, 'position_sample_id', None), self.session.session_id,
                {key: self.session.to_dict().get(key) for key in
                 ('active', 'started', 'task_kind', 'correlation_valid', 'cancel_requested')},
            )
            self.notify(reason)

    def external_command(self, direction: str, *, door_moving: bool = False) -> None:
        """Yield to external movement; a settled closed-door stop grants no authority."""
        if direction == 'open' and self.phase in ('opening', 'return_opening'):
            return
        if (direction == 'stop' and not door_moving and not self._pending and not self._operations
            and not self._closed
            and not self._pause_session and not self._closing_for_exit
            and self.phase in ('idle', 'outside_closed', 'waiting_close_permission', 'blocked', 'manual_hold')):
            self.state = self.evidence.observe(self.clock(), **self.door_observation())
            if self.state.closed_confirmed is True:
                # Some covers use stop at rest to release their clutch. It
                # neither cancels a wait nor authorizes future robot movement.
                return
        if self._no_conflict_for_external(direction, door_moving=door_moving):
            # Nothing is running that this command could fight. Record it and let
            # it happen: the point of holding is to avoid stealing the door from a
            # person, not to punish a later schedule for an earlier manual move.
            _LOGGER.info(
                'Garage control observed external %s with no conflict: phase=%s state=%s '
                'fresh=%s trusted=%s outside=%s session=%s',
                direction, self.phase, getattr(self.robot, 'state', None),
                getattr(self.robot, 'fresh', None), getattr(self.robot, 'position_trusted', None),
                getattr(self.robot, 'outside_danger_zone', None), self.session.session_id,
            )
            return
        self.fault('manual_override', manual=True)

    def _no_conflict_for_external(self, direction: str, *, door_moving: bool) -> bool:
        """Is this external command purely an observation, or does it collide?

        The judgement used to be "did we issue it". That is the wrong question:
        an external open at an idle controller on a settled door has nothing to
        collide with, while the same open during a task or a moving door can hurt
        someone. Size the collision, not the caller.

        Stops are deliberately excluded. Releasing the clutch only makes sense at
        a trusted closed endpoint (where the door cannot fall), and that case has
        its own, stricter exemption above. Granting stops this wider one would
        release the motor of a heavy door whose position we cannot vouch for.
        """
        if direction not in ('open', 'close'):
            # 'stop' keeps its endpoint-checked exemption; 'move' (set_position /
            # toggle) moves in a direction that depends on where the door already
            # is, so there is no "no conflict" form of it and it never qualifies
            # (2026-10-15 spec: 不得借用停止豁免).
            return False
        if self._closed or self.reason:
            return False
        if self._pending or self._operations:
            return False
        if self._pause_session or self._closing_for_exit:
            return False
        if self.phase not in _NO_CONFLICT_PHASES:
            return False
        if door_moving:
            return False
        state = self.state
        if state is None or not state.cover_available:
            return False
        reasons = {state.open.reason, state.close.reason}
        return not (reasons & _BLOCKING_ENDPOINT_REASONS)


    def cancel_pending_for_user(self) -> None:
        """Revoke queued movement before forwarding a user's stop or pause."""
        if self.binding.get('task_door_mode') == 'after_exit' and self.session.active:
            self._user_hold = True
            if not self._pending and self.phase != 'awaiting_start' and not self.reason:
                self._pause_session = None
                self._exit_due = False
                self.phase = 'manual_hold'
        if self.reason and self._pending:
            self.operation_id += 1
            self._pause_session = None
        if not self.reason and (self._pending or self.phase in (
            'awaiting_start', 'pausing_return', 'return_opening', 'resuming_return',
        )):
            self.fault('operation_cancelled', manual=True)

    def shutdown(self) -> None:
        self._closed = True
        self._pause_session = None
        self.operation_id += 1
        self.evidence.invalidate('control_unloaded')

    def _check(self, operation: int | None = None) -> None:
        if self._closed:
            raise GarageControlError('control_unloaded')
        if self.reason:
            raise GarageControlError(self.reason)
        if operation is not None and operation != self.operation_id:
            raise GarageControlError('operation_cancelled')

    def reset(self) -> None:
        """Reconcile on explicit request, preserving any outstanding task."""
        if self._pending or self._operations:
            raise GarageControlError('operation_in_progress')
        self.refresh()
        if not self.robot.fresh or not self.door_observation().get('cover_available'):
            raise GarageControlError('feedback_unavailable')
        self._preflight('open')
        if self.session.to_dict().get('correlation_valid') is False:
            raise GarageControlError('session_recovery_required')
        if not self.session.known_idle:
            terminal_goto = getattr(self.session, 'task_kind', None) == 'goto' and self.session.ended
            if not ((self.session.active and self.session.started) or terminal_goto):
                raise GarageControlError('session_recovery_required')
            if self.session.reason == 'session_recovery_required':
                self.session.reconcile_active(self.robot)
        self.reason = None
        self._observation_deadline = None
        self._observation_session_id = None
        self._return_brake_attempted = False
        # An active task without passage needs a new explicit request. Reset
        # must not restart its door operation or let background ticks relock it.
        self._user_hold = self.session.active and self.passage_allowed is not True
        self.notify(None)
        self.phase = 'manual_hold' if self._user_hold else ('task_open' if self.session.active else 'idle')

    def _acquire(self) -> int:
        self._check()
        if self._pending or self._operations:
            raise GarageControlError('operation_in_progress')
        self._pending = True
        self.operation_id += 1
        self._pending_operation = self.operation_id
        self._operations.add(self.operation_id)
        return self.operation_id

    def _release(self, operation: int) -> None:
        """An obsolete operation must not release a newer safety pause."""
        self._operations.discard(operation)
        if self._pending_operation == operation:
            self._pending = False
            self._pending_operation = None

    def _preflight(self, direction: str) -> None:
        """Reject already invalid sources before sending a physical command."""
        endpoint = self.state.open if direction == 'open' else self.state.close
        reasons = {self.state.open.reason, self.state.close.reason}
        reason = 'endpoint_conflict' if 'endpoint_conflict' in reasons else endpoint.reason
        if reason in ('endpoint_conflict','feedback_unavailable','restored_snapshot','invalid_configuration','invalid_clock'):
            self.fault(reason)
            raise GarageControlError(reason)

    async def _wait(self, direction: str, operation: int) -> None:
        deadline = self.clock() + self.binding['operation_timeout_seconds']
        while True:
            self._check(operation)
            self.refresh()
            if direction == 'open' and self._pause_session and (
                not self.robot.fresh or self.robot.state != 'paused'
            ):
                raise GarageControlError('pause_not_observed')
            if direction == 'close':
                if self._closing_for_exit and self._is_returning():
                    raise _ReturnPreempted
                if not self.close_allowed():
                    if self.supports_stop():
                        await self.move_door('stop', operation)
                    raise GarageControlError('close_not_allowed')
                if not (self._exit_can_close() if self._closing_for_exit else self.session.can_close):
                    if self._closing_for_exit and not self._exit_contradicted():
                        if self.state.closed_confirmed is True:
                            # Accept a physical endpoint, never robot authority.
                            self._observation_deadline = deadline
                            self._observation_session_id = self.session.session_id
                            return
                        await self._recover_close_observation(operation, deadline)
                        continue
                    else:
                        if self.supports_stop():
                            await self.move_door('stop', operation)
                        raise GarageControlError('task_end_unconfirmed')
            endpoint = self.state.open if direction == 'open' else self.state.close
            if endpoint.reason in ('endpoint_conflict', 'feedback_unavailable'):
                raise GarageControlError(endpoint.reason)
            source = self.binding.get(f'{direction}_source')
            estimated = source == 'timed' or (
                source == 'position' and self.binding.get('position_kind') == 'estimated'
            )
            if estimated and (self.state.operation_id != operation or self.state.direction != direction):
                raise GarageControlError('operation_cancelled')
            if direction == 'open':
                ready = self.passage_allowed is True
            else:
                ready = endpoint.reached is True and (
                    endpoint.quality == 'confirmed'
                    or (endpoint.quality == 'estimated' and self.binding.get('allow_estimated') is True)
                )
            self._check(operation)
            if ready:
                return
            if self.clock() >= deadline:
                raise GarageControlError(f'{direction}_timeout')
            await self.sleep(min(0.25, deadline - self.clock()))

    async def _recover_close_observation(self, operation: int, deadline: float) -> None:
        """Stop our close before waiting for data; never extend its deadline."""
        session_id = self.session.session_id
        stopped = self.supports_stop()
        self.evidence.invalidate('observation_wait')
        _LOGGER.warning('Garage close interrupted for observation: operation=%s fresh=%s trusted=%s outside=%s',
                        operation, self.robot.fresh, self.robot.position_trusted,
                        getattr(self.robot, 'outside_danger_zone', None))
        if stopped:
            try:
                await self._move_before_deadline('stop', operation, deadline)
            except GarageControlError:
                raise
            except Exception:
                self._check(operation)
                _LOGGER.warning('Garage stop failed; reversing our close to open', exc_info=True)
                stopped = False
        self._check(operation)
        if not stopped:
            await self._move_before_deadline('open', operation, deadline)
            self._check(operation)
            self.evidence.note_command('open', self.clock(), operation)
        while True:
            self._check(operation)
            self.refresh()
            if self._is_returning():
                raise _ReturnPreempted
            if self.session.session_id != session_id or self._exit_contradicted():
                raise GarageControlError('task_end_unconfirmed')
            if not self.close_allowed():
                raise GarageControlError('close_not_allowed')
            endpoint = self.state.close if stopped else self.state.open
            if endpoint.reason in ('feedback_unavailable', 'endpoint_conflict'):
                raise GarageControlError(endpoint.reason)
            if self.clock() >= deadline:
                raise GarageControlError('observation_timeout')
            if self._exit_can_close():
                if stopped:
                    self.evidence.invalidate('closing')
                    await self._move_before_deadline('close', operation, deadline)
                    self._check(operation)
                    self.evidence.note_command('close', self.clock(), operation)
                    return
                if self.passage_allowed is True:
                    self._exit_due = self._departure_armed = False
                    raise _DepartureCloseAborted
            await self.sleep(min(.25, deadline - self.clock()))

    async def _move_before_deadline(self, direction: str, operation: int, deadline: float) -> None:
        self._check(operation)
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise GarageControlError('observation_timeout')
        timeout = asyncio.timeout(remaining)
        try:
            async with timeout:
                await self.move_door(direction, operation)
        except TimeoutError:
            if timeout.expired():
                raise GarageControlError('observation_timeout') from None
            raise
        self._check(operation)

    async def _open(self, operation: int) -> None:
        self.refresh()
        self._preflight('open')
        if self.passage_allowed is True:
            return
        if not self.door_observation().get('cover_available'):
            raise GarageControlError('feedback_unavailable')
        self.phase = 'return_opening' if self._pause_session else 'opening'
        self.evidence.invalidate('opening')
        await self.move_door('open', operation)
        self._check(operation)
        self.evidence.note_command('open', self.clock(), operation)
        await self._wait('open', operation)

    async def async_execute(
        self, command: str, send: Callable[[], Awaitable[Any]], *,
        new_task: bool = False, resume: bool = False,
    ) -> Any:
        """Run the original caller's command only while its passage is valid."""
        self._check()
        self.refresh()
        if resume and not (self.session.active and self.session.started):
            raise GarageControlError('session_recovery_required')
        if new_task and self.session.active:
            raise GarageControlError('task_already_active')
        if new_task and not self.session.can_begin:
            raise GarageControlError('session_recovery_required')
        operation = self._acquire()
        try:
            self._user_hold = False
            await self._open(operation)
            self.refresh()
            self._check(operation)
            if not self._robot_is_outside():
                # The robot still has to drive through the doorway, so the door
                # must be usable before any clean command.
                if self.passage_allowed is not True:
                    raise GarageControlError('feedback_unavailable')
            if not self.robot.fresh:
                raise GarageControlError('position_untrusted')
            if new_task:
                if not self.session.can_begin:
                    raise GarageControlError('session_recovery_required')
                self.session.begin(self.robot.summary, self.robot.utc, command)
                await self.persist()
            elif command == 'app_charge' and getattr(self.session, 'task_kind', None) == 'goto':
                self.session.redirect_to_return(self.robot.utc)
                await self.persist()
            self._check(operation)
            # No await between this final observation/check and invoking send.
            self.refresh()
            if not self._robot_is_outside() and self.passage_allowed is not True:
                raise GarageControlError('feedback_unavailable')
            self._check(operation)
            self.phase = 'awaiting_start'
            self._expect_resume = resume
            self._resume_sample = getattr(self.robot, 'sample_id', None)
            self._start_deadline = self.clock() + self.start_timeout
            result = await send()
            self._check(operation)
            return result
        except asyncio.CancelledError:
            self.fault('operation_cancelled')
            raise
        except Exception as err:
            if not self.reason:
                self.fault(getattr(err, 'reason', None) or 'command_failed')
            raise
        finally:
            self._release(operation)

    async def async_request_close(self) -> None:
        """The single protected close entry for final docking and family policies."""
        self.refresh()
        self._check()
        self._preflight('close')
        if not self.close_allowed():
            raise GarageControlError('close_not_allowed')
        if not self.session.can_close:
            raise GarageControlError('task_end_unconfirmed')
        operation = self._acquire()
        try:
            if self.state.closed_confirmed is not True:
                self.phase = 'closing'
                self.evidence.invalidate('closing')
                await self.move_door('close', operation)
                self._check(operation)
                self.evidence.note_command('close', self.clock(), operation)
                await self._wait('close', operation)
            self.phase = 'idle'
            await self.persist()
        except asyncio.CancelledError:
            self.fault('operation_cancelled')
            raise
        except Exception as err:
            if not self.reason:
                self.fault(getattr(err, 'reason', None) or 'command_failed')
            raise
        finally:
            self._release(operation)

    async def async_run_controlled_exit(self, settings: dict) -> None:
        """Drive straight out of the cabinet, then shut the door behind the robot.

        Why this shape of sequence exists
        ---------------------------------
        The cabinet is the robot's only way in or out: the door must be open for
        it to leave and closed while it cleans. Left to plan its own departure,
        the robot's docking manoeuvre swings far enough right to graze the closed
        door beside the cabinet (measured 2026-10-25, 373mm of clearance).

        An earlier design watched for ``docked -> cleaning`` and paused the robot
        to open the door. That failed in the field: the state change arrives after
        the robot has already moved, so the pause landed mid-manoeuvre in a tight
        opening and the resumed robot spun in place forever. Nothing here
        interrupts the robot: it is given one waypoint and driven there.

        The door is held open for the whole trip (``_update_after_exit`` returns
        early while ``phase == 'controlled_exit'``), then closed at the waypoint,
        before the clean command is issued.

        Failure is terminal for this attempt: the caller must not fall through to
        sending the clean command, because a shut door is the whole point and a
        robot that has not cleared the cabinet must not start working inside it.
        """
        target = (settings['x'], settings['y'])
        timeout = settings['timeout']
        self.refresh()
        self._check()
        self._preflight('open')
        if not (self.robot.fresh and self.robot.position_trusted):
            self.fault('position_untrusted')
            raise GarageControlError('position_untrusted')
        operation = self._acquire()
        self._controlled_exit_target = target
        self._controlled_exit_deadline = self.clock() + timeout
        try:
            await self._open(operation)
            self._check(operation)
            if self.send_robot is None:
                raise GarageControlError('unsupported_controlled_exit')
            self.phase = 'controlled_exit'
            self.evidence.invalidate('opening')
            await self.send_robot('app_goto_target', list(target))
            await self._wait_controlled_exit(operation)
            await self._close_after_controlled_exit(operation)
        except asyncio.CancelledError:
            self.fault('operation_cancelled')
            raise
        except Exception as err:
            if not self.reason:
                self.fault(getattr(err, 'reason', None) or 'controlled_exit_failed')
            raise
        finally:
            self._controlled_exit_target = None
            self._controlled_exit_deadline = None
            self._release(operation)

    async def _wait_controlled_exit(self, operation: int) -> None:
        """Wait for the waypoint using measured coordinates, never a state guess.

        Position is the only direct fact here. ``going_to_target`` cannot tell
        "arrived" from "stopped for some other reason", and a fixed sleep cannot
        tell either. The tolerance exists because docking precision is not zero.
        """
        from .garage_config import CONTROLLED_EXIT_TOLERANCE_MM

        while True:
            self._check(operation)
            self.refresh()
            if self._controlled_exit_arrived():
                return
            if self._controlled_exit_deadline is not None and self.clock() >= self._controlled_exit_deadline:
                raise GarageControlError('controlled_exit_failed')
            await asyncio.sleep(0.2)

    def _robot_is_outside(self) -> bool:
        """Return whether the robot is confirmed past the danger zone already.

        When true, a clean command does not need the door to be passable: the
        robot is not going to drive through the doorway, it is already on the far
        side of it. This is what makes "shut the door, then start cleaning" a
        coherent sequence instead of a deadlock.

        Every clause is load-bearing:

        * ``fresh`` and ``position_trusted`` -- an old or contradictory reading
          must never be enough to wave a robot through a shut door.
        * ``outside_danger_zone is True`` -- not None, not None-because-unknown.
          The observation layer already refuses to compute this from a
          dock-substituted position unless the charger marker itself sits inside
          the zone (see ``garage_runtime.robot_observation``), so a docked robot
          cannot masquerade as "outside" here.
        * ``not docked`` -- a robot sitting on its dock has not left, whatever a
          stale coordinate says.
        """
        return bool(
            getattr(self.robot, 'fresh', False)
            and getattr(self.robot, 'position_trusted', False) is True
            and getattr(self.robot, 'outside_danger_zone', None) is True
            and not getattr(self.robot, 'docked', False)
        )

    def _controlled_exit_arrived(self) -> bool:
        if not (self.robot.fresh and self.robot.position_trusted):
            return False
        target = self._controlled_exit_target
        if target is None:
            return False
        position = getattr(self.robot, 'position', None)
        x = getattr(position, 'x', None)
        y = getattr(position, 'y', None)
        if x is None or y is None:
            return False
        from .garage_config import CONTROLLED_EXIT_TOLERANCE_MM
        return math.hypot(x - target[0], y - target[1]) <= CONTROLLED_EXIT_TOLERANCE_MM

    async def _close_after_controlled_exit(self, operation: int) -> None:
        """Shut the door at the waypoint, before the clean command is sent.

        Deliberately not ``async_request_close`` (it demands
        ``session.can_close`` = a settled dock, which is false here) and not
        ``_close_after_departure`` (its ``_exit_can_close`` demands a cleaning
        state, while the robot is at the waypoint). The conditions below are the
        same facts those paths rely on -- robot really outside, feedback usable,
        no concurrency -- only the trigger moment differs.
        """
        self.refresh()
        self._check(operation)
        if self.robot.outside_danger_zone is not True:
            raise GarageControlError('controlled_exit_failed')
        if not self.state.cover_available:
            raise GarageControlError('feedback_unavailable')
        reasons = {self.state.open.reason, self.state.close.reason}
        if reasons & _BLOCKING_ENDPOINT_REASONS:
            raise GarageControlError('feedback_unavailable')
        if not self.close_allowed():
            raise GarageControlError('close_not_allowed')
        self._preflight('close')
        if self.state.closed_confirmed is not True:
            self.phase = 'closing'
            self.evidence.invalidate('closing')
            await self.move_door('close', operation)
            self._check(operation)
            self.evidence.note_command('close', self.clock(), operation)
            await self._wait('close', operation)
        self.phase = 'idle'
        await self.persist()

    async def async_request_open(self) -> None:
        """The single protected open entry for explicit household passage requests.

        Symmetric to ``async_request_close`` with two deliberate differences:

        * The family permission is NOT consulted. That permission only restricts
          closing (REQ-9); a household that blocked automatic closing has not
          asked to be locked in.
        * ``session.can_close`` is NOT consulted. It demands a settled dock and a
          known-idle task, which is exactly what a "somebody wants to walk in"
          request contradicts. Concurrency is handled by ``_acquire`` and by the
          phase rejections instead.

        An already-passable door is a no-op: the point is to have a door you can
        walk through, not to make the motor move.

        No separate ``cover_available`` guard here: ``refresh()`` folds that fact
        into the endpoint evidence, so ``_preflight`` already refuses with the same
        ``feedback_unavailable`` reason. Duplicating it looked defensive but was
        unreachable -- a guard no test can reach is a guard that will rot.
        """
        self.refresh()
        self._check()
        self._preflight('open')
        if self.passage_allowed is True:
            return
        operation = self._acquire()
        try:
            self.phase = 'return_opening' if self._pause_session else 'opening'
            self.evidence.invalidate('opening')
            await self.move_door('open', operation)
            self._check(operation)
            self.evidence.note_command('open', self.clock(), operation)
            await self._wait('open', operation)
            self.phase = 'idle'
            await self.persist()
        except asyncio.CancelledError:
            self.fault('operation_cancelled')
            raise
        except Exception as err:
            if not self.reason:
                self.fault(getattr(err, 'reason', None) or 'command_failed')
            raise
        finally:
            self._release(operation)

    def _is_returning(self) -> bool:
        return bool(self.robot.fresh and self.robot.state in _RETURN_STATES and not self.robot.docked)

    def _exit_contradicted(self) -> bool:
        """Separate positive contradictions from unavailable observations."""
        facts = self.session.to_dict()
        if (not self.session.active or not self.session.started or self.session.task_kind != 'clean'
                or (self._closing_session_id is not None and self.session.session_id != self._closing_session_id)
                or facts.get('correlation_valid') is False
                or facts.get('cancel_requested')):
            return True
        if self.robot.fresh and self.robot.docked:
            return True
        outside = getattr(self.robot, 'outside_danger_zone', None)
        if self.robot.fresh and self.robot.position_trusted and outside is False:
            return True
        # A new state that is neither cleaning nor transport is a real change of
        # intent; an unchanged cleaning state proves nothing either way.
        return self.robot.fresh and self.robot.state not in _CLEANING_STATES

    def _observation_missing(self) -> bool:
        return (not self.robot.fresh or not self.robot.position_trusted
                or getattr(self.robot, 'outside_danger_zone', None) is None)

    def _wait_outside_observation(self) -> None:
        """Wait without issuing commands; full revalidation ends this interval."""
        if self._observation_deadline is None:
            self._observation_deadline = self.clock() + self.binding['operation_timeout_seconds']
            self._observation_session_id = self.session.session_id
            _LOGGER.warning('Garage observation waiting: phase=%s fresh=%s trusted=%s outside=%s',
                            self.phase, self.robot.fresh, self.robot.position_trusted,
                            getattr(self.robot, 'outside_danger_zone', None))
        if self.clock() >= self._observation_deadline:
            self.fault('observation_timeout')

    def _reconcile_observation_wait(self) -> None:
        if self._observation_deadline is None:
            return
        if self.session.session_id != self._observation_session_id:
            self.fault('session_recovery_required')
        elif self.clock() >= self._observation_deadline:
            self.fault('observation_timeout')
        elif not self._observation_missing():
            self._observation_deadline = self._observation_session_id = None

    def _exit_can_close(self) -> bool:
        return bool(self.session.active and self.session.started
                    and getattr(self.session, 'task_kind', None) == 'clean'
                    and self.session.to_dict().get('correlation_valid') is not False
                    and not self.session.to_dict().get('cancel_requested')
                    and self.robot.fresh and self.robot.position_trusted
                    and getattr(self.robot, 'outside_danger_zone', None) is True
                    and self.robot.state in _CLEANING_STATES and not self.robot.docked)

    def _return_context(self) -> tuple[Any, ...]:
        """Capture record identity, not transient paused flags or command echoes."""
        summary = self.robot.summary
        def value(obj: Any, key: str) -> Any:
            return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)
        records, count = value(summary, 'records'), value(summary, 'clean_count')
        if not isinstance(records, list) or type(count) is not int:
            raise GarageControlError('task_end_unconfirmed')
        if not self.session.matches_current_records(summary, self.robot.utc):
            raise GarageControlError('session_recovery_required')
        record = value(summary, 'last_clean_record')
        return (tuple(records), count, *(value(record, key) for key in ('begin', 'end', 'complete', 'finish_reason', 'error')))

    def _check_pause_context(self, context: tuple[Any, ...]) -> None:
        data = self.session.to_dict()
        same_record = self._return_context() == context
        completion = self.session.matches_current_completion(self.robot.summary, self.robot.utc)
        if (self._pause_session != self.session.session_id or not self.session.active
            or not self.session.started or data.get('correlation_valid') is False
            or data.get('cancel_requested') or not (same_record or completion)):
            raise GarageControlError('session_recovery_required')

    async def _wait_robot(self, operation: int, sample: Any, *, paused: bool,
                          context: tuple[Any, ...] | None) -> None:
        deadline = self.clock() + self.start_timeout
        while True:
            self._check(operation)
            self.refresh()
            # Braking is physical protection. Task lineage authorizes only
            # resuming movement, never whether we wait for the robot to stop.
            if not paused:
                if context is None:
                    raise GarageControlError('session_recovery_required')
                self._check_pause_context(context)
            if not paused and self.passage_allowed is not True:
                raise GarageControlError('feedback_unavailable')
            new = getattr(self.robot, 'sample_id', None) != sample
            ready = self.robot.state == 'paused' if paused else self.robot.state in _RETURN_STATES
            if self.robot.fresh and new and ready and not self.robot.docked:
                return
            if self.clock() >= deadline:
                raise GarageControlError('pause_not_observed' if paused else 'resume_not_observed')
            await self.sleep(min(.25, deadline - self.clock()))

    async def _return_through_door(self) -> None:
        """Only our observed pause may be followed by an automatic resume."""
        operation = self._acquire()
        self._return_brake_attempted = True
        try:
            if self.send_robot is None:
                raise GarageControlError('unsupported_return_interlock')
            self.refresh()
            self._check(operation)
            if not self._is_returning():
                raise GarageControlError('session_recovery_required')
            # Keep the initial authorization result immutable: a later good
            # summary cannot erase an initially invalid/cancelled task.
            context = None
            authority_error = None
            try:
                context = self._return_context()
            except GarageControlError as err:
                authority_error = err.reason
            self._pause_session = self.session.session_id
            self._exit_due = False
            self._departure_armed = False
            self.phase = 'pausing_return'
            sample = getattr(self.robot, 'sample_id', None)
            await self.send_robot('app_pause')
            await self._wait_robot(operation, sample, paused=True, context=context)
            self._check(operation)
            if authority_error is not None:
                raise GarageControlError(authority_error)
            if context is None:
                raise GarageControlError('session_recovery_required')
            self._check_pause_context(context)
            if self._user_hold:
                raise GarageControlError('operation_cancelled')
            await self._open(operation)
            # No authority captured before the pause can be adopted later.
            self.refresh()
            self._check(operation)
            self._check_pause_context(context)
            if not self.robot.fresh or self.robot.state != 'paused' or self.passage_allowed is not True:
                raise GarageControlError('pause_not_observed')
            self.phase = 'resuming_return'
            sample = getattr(self.robot, 'sample_id', None)
            await self.send_robot('app_resume')
            await self._wait_robot(operation, sample, paused=False, context=context)
            self.phase = 'returning'
            self._return_brake_attempted = False
        except asyncio.CancelledError:
            self.fault('operation_cancelled')
            raise
        except Exception as err:
            if not self.reason:
                self.fault(getattr(err, 'reason', None) or 'command_failed')
        finally:
            if self._pending_operation == operation:
                self._pause_session = None
            self._release(operation)

    async def _brake_return_only(self) -> None:
        """A fault may deny movement authority, never physical return braking."""
        if self._return_brake_attempted or self._pending or self._closed:
            return
        self._return_brake_attempted = True
        self._pending = True
        self.operation_id += 1
        operation = self.operation_id
        self._pending_operation = operation
        self._operations.add(operation)
        sample = getattr(self.robot, 'sample_id', None)
        deadline = self.clock() + self.start_timeout
        try:
            if self.send_robot is None:
                raise GarageControlError('unsupported_return_interlock')
            await self.send_robot('app_pause')
            while True:
                if self._closed or operation != self.operation_id:
                    raise GarageControlError('operation_cancelled')
                self.refresh()
                if (self.robot.fresh and self.robot.state == 'paused' and not self.robot.docked
                        and getattr(self.robot, 'sample_id', None) != sample):
                    if not self.reason and not self._user_hold:
                        self.fault('session_recovery_required')
                    return
                if self.clock() >= deadline:
                    raise GarageControlError('pause_not_observed')
                await self.sleep(min(.25, deadline - self.clock()))
        except asyncio.CancelledError:
            if not self.reason:
                self.fault('operation_cancelled')
            raise
        except Exception as err:
            if not self.reason:
                self.fault(getattr(err, 'reason', None) or 'command_failed')
            else:
                _LOGGER.warning('Garage safety pause failed; original fault retained: reason=%s pause_error=%s',
                                self.reason, getattr(err, 'reason', None) or type(err).__name__)
        finally:
            self._release(operation)

    async def _handle_return(self) -> None:
        facts = self.session.to_dict()
        if (self.reason or self._user_hold or not self.session.active or not self.session.started
                or facts.get('cancel_requested') or facts.get('correlation_valid') is False):
            await self._brake_return_only()
        elif not self._return_brake_attempted:
            await self._return_through_door()

    async def async_brake_pending_return(self) -> None:
        """Safety-only entry: never advance an ordinary task beside a door RPC."""
        if self._closed or not self._pending or not self._closing_for_exit:
            return
        self.refresh()
        if not self._is_returning() or self.passage_allowed is True:
            return
        # The old RPC may still finish. Revoke it without replacing an existing
        # fault/manual hold, and let only the safety pause take ownership.
        if not self.reason:
            self.fault('operation_cancelled')
        # Release ownership, not the old operation's lifetime. Its finally is
        # the only place that retires it and permits a later explicit reset.
        self._pending = False
        self._pending_operation = None
        self._closing_for_exit = False
        await self._brake_return_only()

    async def _close_after_departure(self) -> None:
        """Close on a real crossing without adding a pause to HA departure."""
        operation = self._acquire()
        returning = False
        self._closing_for_exit = True
        session_id = self._closing_session_id = self.session.session_id
        try:
            self.refresh()
            self._check(operation)
            if not self._exit_can_close() or not self.close_allowed():
                return
            self._preflight('close')
            self.phase = 'closing'
            self.evidence.invalidate('closing')
            await self.move_door('close', operation)
            self._check(operation)
            self.evidence.note_command('close', self.clock(), operation)
            await self._wait('close', operation)
            self._exit_due = False
            self.phase = 'outside_closed'
        except _ReturnPreempted:
            returning = True
            if self.session.session_id != session_id:
                self.fault('session_recovery_required')
            # Pause the robot first. Once confirmed, open_cover reverses our
            # unfinished close; an optional stop RPC must not delay braking.
        except _DepartureCloseAborted:
            self._exit_due = self._departure_armed = False
            self._observation_deadline = None
            self._observation_session_id = None
            self.phase = 'task_open'
        except asyncio.CancelledError:
            self.fault('operation_cancelled')
            raise
        except Exception as err:
            if not self.reason:
                self.fault(getattr(err, 'reason', None) or 'command_failed')
        finally:
            self._closing_for_exit = False
            self._closing_session_id = None
            self._release(operation)
        if returning or (self.reason and self._is_returning()):
            await self._handle_return()

    async def _update_after_exit(self) -> bool:
        if self.binding.get('task_door_mode') != 'after_exit':
            return False
        if self.phase == 'controlled_exit':
            # The robot is driving out of the cabinet towards the waypoint.
            # Closing now would shut the door on it. The same run closes the door
            # itself once the waypoint is reached.
            return True
        if self._user_hold:
            self.phase = 'manual_hold'
            return True
        if self.phase == 'outside_closed' and self.session.active:
            if self.state.close.reason in ('feedback_unavailable', 'endpoint_conflict'):
                self.fault(self.state.close.reason)
            elif self._observation_missing():
                self._wait_outside_observation()
            else:
                self._observation_deadline = None
                self._observation_session_id = None
                if getattr(self.robot, 'outside_danger_zone', None) is False and not self.robot.docked:
                    self.fault('position_untrusted')
            return True
        if self._exit_due and self._exit_can_close() and self.close_allowed():
            await self._close_after_departure()
            return True
        return False

    async def async_update(self) -> None:
        """Advance observations and deadlines, without inventing robot commands."""
        if self._closed:
            return
        self.refresh()
        self._reconcile_observation_wait()
        if self._pending:
            await self.async_brake_pending_return()
            return
        if self.robot.fresh and self.robot.state not in _RETURN_STATES:
            self._return_brake_attempted = False
        if (self.binding.get('task_door_mode') == 'after_exit'
                and self._is_returning() and self.passage_allowed is not True):
            await self._handle_return()
            return
        if self.reason:
            return
        if self.session.to_dict().get('correlation_valid') is False:
            self.fault(self.session.reason or 'session_recovery_required')
            return
        if self.phase == 'awaiting_start':
            resumed = (self._expect_resume and self.robot.fresh and self.robot.state in _RESUMED_STATES
                       and bool(getattr(self.robot,'in_cleaning',None) or getattr(self.robot,'in_returning',None))
                       and getattr(self.robot,'sample_id',None) != self._resume_sample)
            preparing = (not self._expect_resume and self.session.active and self.robot.fresh
                         and self.robot.state in _PREPARING_STATES and bool(getattr(self.robot,'in_cleaning',None)))
            if (self.session.started and not self._expect_resume) or resumed or preparing:
                self.phase = 'task_open'
                self._start_deadline = None
            elif self._start_deadline is not None and self.clock() >= self._start_deadline:
                self.fault('resume_not_observed' if self._expect_resume else 'start_not_observed')
                return
        if await self._update_after_exit():
            return
        if self.session.ended and self.session.can_close and self.phase in (
            'task_open', 'returning', 'docked_intermission', 'waiting_close_permission',
        ):
            if not self.close_allowed():
                self.phase = 'waiting_close_permission'
                return
            await self.async_request_close()
        elif self.session.active and self.phase in ('task_open', 'returning', 'docked_intermission'):
            if self.passage_allowed is not True:
                self.fault('feedback_unavailable')
            elif self.robot.docked:
                self.phase = 'docked_intermission'
            elif self.robot.state in ('docking', 'returning_home', 'going_to_wash_the_mop'):
                self.phase = 'returning'
            else:
                self.phase = 'task_open'
        elif self.session.ended and getattr(self.session, 'task_kind', None) == 'goto':
            self.phase = 'idle'
