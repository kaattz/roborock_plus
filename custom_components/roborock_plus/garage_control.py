"""Single-owner garage workflow. All device I/O is supplied by the HA adapter."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

_LOGGER = logging.getLogger(__name__)

_PREPARING_STATES = frozenset({'starting','washing_the_mop','washing_the_mop_2','attaching_the_mop','detaching_the_mop'})
_RESUMED_STATES = frozenset({'cleaning','spot_cleaning','zoned_cleaning','segment_cleaning',
                            'going_to_target','docking','returning_home','going_to_wash_the_mop'})
_CLEANING_STATES = frozenset({'cleaning', 'spot_cleaning', 'zoned_cleaning', 'segment_cleaning'})
_RETURN_STATES = frozenset({'docking', 'returning_home', 'going_to_wash_the_mop'})


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
        send_robot: Callable[[str], Awaitable[Any]] | None = None,
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
        self.operation_id = 0
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
            token = getattr(self.robot, 'position_sample_id', None)
            outside = getattr(self.robot, 'outside_danger_zone', None)
            if not (self.robot.fresh and self.robot.position_trusted) or outside is None:
                self._departure_armed = False
                self._exit_due = False
            elif token is not None and token != self._position_sample:
                self._position_sample = token
                if outside is False:
                    self._departure_armed = True
                    self._exit_due = False
                elif self._departure_armed:
                    self._exit_due = True
                    self._departure_armed = False

    def fault(self, reason: str, *, manual: bool = False) -> None:
        """Invalidate waiting operations; recovery of a sensor is not a reset."""
        previous_phase = self.phase
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
        self.fault('manual_override', manual=True)

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
            if self.passage_allowed is not True:
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
