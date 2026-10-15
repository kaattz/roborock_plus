"""Single-owner garage workflow. All device I/O is supplied by the HA adapter."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

_PREPARING_STATES = frozenset({'starting','washing_the_mop','washing_the_mop_2','attaching_the_mop','detaching_the_mop'})
_RESUMED_STATES = frozenset({'cleaning','spot_cleaning','zoned_cleaning','segment_cleaning',
                            'going_to_target','docking','returning_home','going_to_wash_the_mop'})


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
        self.phase = 'recovering'
        self.reason: str | None = None
        self.operation_id = 0
        self.state: Any = None
        self.robot: Any = None
        self._pending = False
        self._closed = False
        self._start_deadline: float | None = None
        self._expect_resume = False
        self._resume_sample: Any = None

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

    def fault(self, reason: str, *, manual: bool = False) -> None:
        """Invalidate waiting operations; recovery of a sensor is not a reset."""
        self.operation_id += 1
        self.evidence.invalidate(reason)
        changed = self.reason != reason
        self.reason = reason
        self.phase = 'manual_hold' if manual else 'blocked'
        if changed:
            self.notify(reason)

    def external_command(self, direction: str) -> None:
        """Yield to external control, except an already pending same-direction open."""
        if direction == 'open' and self.phase == 'opening':
            return
        self.fault('manual_override', manual=True)

    def cancel_pending_for_user(self) -> None:
        """Revoke queued movement before forwarding a user's stop or pause."""
        if not self.reason and (self._pending or self.phase == 'awaiting_start'):
            self.fault('operation_cancelled', manual=True)

    def shutdown(self) -> None:
        self._closed = True
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
        if self._pending:
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
        self.notify(None)
        self.phase = 'task_open' if self.session.active else 'idle'

    def _acquire(self) -> int:
        self._check()
        if self._pending:
            raise GarageControlError('operation_in_progress')
        self._pending = True
        self.operation_id += 1
        return self.operation_id

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
            if direction == 'close':
                if not self.close_allowed():
                    if self.supports_stop():
                        await self.move_door('stop', operation)
                    raise GarageControlError('close_not_allowed')
                if not self.session.can_close:
                    if self.supports_stop():
                        await self.move_door('stop', operation)
                    raise GarageControlError('task_end_unconfirmed')
            endpoint = self.state.open if direction == 'open' else self.state.close
            if endpoint.reason in ('endpoint_conflict', 'feedback_unavailable'):
                raise GarageControlError(endpoint.reason)
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

    async def _open(self, operation: int) -> None:
        self.refresh()
        self._preflight('open')
        if self.passage_allowed is True:
            return
        if not self.door_observation().get('cover_available'):
            raise GarageControlError('feedback_unavailable')
        self.phase = 'opening'
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
            self._pending = False

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
            self._pending = False

    async def async_update(self) -> None:
        """Advance observations and deadlines, without inventing robot commands."""
        if self._closed:
            return
        self.refresh()
        if self.reason:
            return
        if self.session.to_dict().get('correlation_valid') is False:
            self.fault(self.session.reason or 'session_recovery_required')
            return
        if self._pending:
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
