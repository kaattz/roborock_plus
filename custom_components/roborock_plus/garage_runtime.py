"""Home Assistant boundary for the single-owner garage workflow.

This layer only translates: Home Assistant states in, ``GarageController``
decisions out. All judgement about *when* a door may move lives in
``garage_door.py``, which is pure and testable without Home Assistant.

There is no task bookkeeping here. Every clean command enters through this
integration, so "is this a task I authorised" is answered by construction. The
machinery that used to track it (session ids, clean-counter baselines,
correlation flags) is gone, and with it the latches that kept refusing to work on
an idle, docked robot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import logging
import math
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, NamedTuple

from .garage_commands import SAFE_DIRECT_COMMANDS
from .garage_config import (
    CONTROLLED_EXIT_LATERAL_WARN_MM,
    GarageConfigError,
    legacy_exit_distance,
    resolve_binding,
    resolve_exit_direction,
    resolve_exit_target,
    validate_binding_ownership,
)
from .garage_door import GarageControlError, GarageController
from .v1_task_state import is_v1_task_active

REGISTRY_KEY = 'roborock_plus_garage_owners'
EVENT_GARAGE_ISSUE = 'roborock_plus_garage_issue'
INITIALIZED_KEY = 'garage_initialized_duids'
_LOGGER = logging.getLogger(__name__)

_NEW_TASK_COMMANDS = frozenset({
    'app_start', 'app_segment_clean', 'app_zoned_clean', 'app_spot', 'app_goto_target',
})
# Commands that begin cleaning. ``app_goto_target`` is deliberately absent: it is
# the vehicle for the controlled exit itself, so treating it as a clean would
# recurse.
_CONTROLLED_EXIT_COMMANDS = frozenset({
    'app_start', 'app_segment_clean', 'app_zoned_clean', 'app_spot',
})
_RESUME_COMMANDS = frozenset({'app_resume', 'app_resume_build_map'})
_DIRECT_COMMANDS = SAFE_DIRECT_COMMANDS


class RobotObservation(NamedTuple):
    """One coherent robot reading. Physical facts only; no task identity.

    ``x``/``y`` are the map coordinates the safety decisions are based on, and are
    present only when ``position_trusted`` is true. They are here because a caller
    asked for them and the field was missing: ``_run_controlled_exit`` read
    ``getattr(robot, 'position', None)``, which is always ``None`` on a NamedTuple
    without that name, so its arrival test could never be satisfied and every
    controlled exit died on its deadline. A missing coordinate must be an
    explicit ``None``, never an absent attribute.
    """

    now: float
    utc: float
    state: Any
    in_cleaning: Any = None
    in_returning: Any = None
    summary: Any = None
    position_trusted: bool = False
    docked: bool = False
    fresh: bool = False
    sample_id: Any = None
    sample_utc: Any = None
    outside_danger_zone: bool | None = None
    position_sample_id: Any = None
    task_active: bool | None = None
    x: float | None = None
    y: float | None = None


def _name(value: Any) -> str:
    return str(getattr(value, 'name', value))


def command_name(command: Any) -> str:
    return str(getattr(command, 'value', command)).lower()


def configured(coordinator: Any) -> bool:
    options = coordinator.config_entry.options
    bindings = options.get('garage_bindings', {})
    return isinstance(bindings, Mapping) and isinstance(bindings.get(coordinator.duid), Mapping) and bindings[coordinator.duid].get('enabled') is True


def check_legacy_ownership(hass: Any, options: Mapping[str, Any], duid: str) -> None:
    """Retired with the flat guard; native bindings settle ownership among themselves."""


class GarageRuntime:
    """Adapt Home Assistant observations to the position-driven door rules."""

    def __init__(
        self, hass: Any, coordinator: Any, binding: Mapping[str, Any], *, store: Any = None,
        zone_provider: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        utc: Callable[[], float] = time.time,
    ) -> None:
        self.hass, self.coordinator = hass, coordinator
        self.entry = coordinator.config_entry
        self.binding = dict(binding)
        self.clock, self.utc = clock, utc
        self._zone_provider = zone_provider or self._zone
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribers: list[Callable[[], None]] = []
        self._contexts: list[str] = []
        #: The task carrying the current clean's door work, if a clean is running.
        self._sequence_task: asyncio.Task | None = None
        #: Sleep between sequence checks. Replaceable so tests can run a whole trip in
        #: no time while the loop keeps its real deadline logic.
        self._sequence_sleep = asyncio.sleep
        self._closed = False
        self._last_issue: str | None = None
        self.stage_result: str | None = None
        #: How far the robot stopped from the last exit waypoint, in mm. ``None``
        #: when no exit has run or the position could not be trusted.
        self.exit_arrival_offset_mm: float | None = None
        self.stage_reason: str | None = None
        self.controller = GarageController(
            binding=self.binding,
            door_observation=self.door_observation,
            robot_observation=self.robot_observation,
            move_door=self.move_door,
            close_allowed=self.close_allowed,
            clock=clock,
        )
        self.controller.phase = 'idle'

    # -- observations --------------------------------------------------------

    def _zone(self) -> Any:
        from .safe_zone_store import get_safe_zone_store
        stored = get_safe_zone_store(self.hass).get_loaded(self.coordinator.duid)
        return None if stored is None else stored.zone

    def door_observation(self) -> dict[str, Any]:
        """Read the cover and its contacts, excluding restored provider values."""
        binding = getattr(self, 'binding', {})
        cover = self.hass.states.get(binding.get('cover_entity_id', ''))
        attrs = {} if cover is None else cover.attributes
        phase = getattr(getattr(self, 'controller', None), 'phase', None)
        own_context = getattr(getattr(cover, 'context', None), 'id', None) in self._contexts
        own_close = phase == 'closing' or (own_context and phase == 'pausing_return')
        available = (
            binding.get('handover_confirmed') is True
            and cover is not None
            and cover.state not in ('unknown', 'unavailable')
            and (cover.state != 'closing' or own_close)
        )
        values: dict[str, Any] = dict(
            cover_available=available,
            position=attrs.get('current_position'),
            restored=attrs.get('restored') is True,
        )
        for direction in ('open', 'close'):
            contact = self.hass.states.get(binding.get(f'{direction}_contact', ''))
            values[f'{direction}_contact'] = (
                None if contact is None or contact.attributes.get('restored') else contact.state
            )
        return values

    def robot_observation(self) -> RobotObservation:
        """Read the robot: state, dock presence, and whether it is past the zone.

        ``outside_danger_zone`` is only computed from a trusted position. The
        charger marker must itself lie inside the zone for a docked robot's
        substituted position to count, so a parked robot can never be mistaken
        for a departed one.

        The resolved coordinate is carried through as well. It is the same number
        ``outside_danger_zone`` was derived from, so handing it to callers costs
        nothing and stops them reading for a field this record does not define.
        """
        c = self.coordinator
        data = getattr(c, 'data', None)
        sample = getattr(c, 'garage_status_sample', None) or {}
        stamp = sample.get('sampled_at')
        sampled = stamp.timestamp() if isinstance(stamp, datetime) else None
        interval = getattr(c, 'update_interval', timedelta(seconds=60))
        max_age = max(1.0, interval.total_seconds() * 2) if isinstance(interval, timedelta) else 120.0
        fresh = bool(getattr(c, 'last_update_success', False) and sampled is not None
                     and 0 <= self.utc() - sampled <= max_age)
        state = str(sample.get('state', 'unknown'))
        trusted = False
        outside: bool | None = None
        position_sample: float | None = None
        position_x: float | None = None
        position_y: float | None = None
        map_stamp = getattr(c, 'last_map_position_update', None)
        if isinstance(map_stamp, datetime) and map_stamp.utcoffset() is not None:
            candidate = map_stamp.timestamp()
            if math.isfinite(candidate) and 0 <= candidate <= self.utc():
                position_sample = candidate
        try:
            resolved = c.resolve_vacuum_position()
            zone = self._zone_provider()
            map_data = getattr(c.properties_api.map_content, 'map_data', None)
            charger = getattr(map_data, 'charger', None)
            numbers = (resolved.x, resolved.y, getattr(charger, 'x', None), getattr(charger, 'y', None))
            valid = all(isinstance(n, (int, float)) and not isinstance(n, bool)
                        and math.isfinite(n) for n in numbers)
            bounds = () if zone is None else (zone.min_x, zone.max_x, zone.min_y, zone.max_y)
            valid_zone = (len(bounds) == 4 and all(
                isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
                for n in bounds
            ) and zone.min_x < zone.max_x and zone.min_y < zone.max_y)
            trusted = bool(resolved.trusted and valid and valid_zone
                           and zone.min_x <= charger.x <= zone.max_x
                           and zone.min_y <= charger.y <= zone.max_y)
            if state in ('charging', 'charging_complete'):
                trusted = trusted and math.hypot(resolved.x - charger.x, resolved.y - charger.y) <= 1000
            if trusted:
                position_x, position_y = resolved.x, resolved.y
            if trusted and fresh and position_sample is not None:
                outside = not (zone.min_x <= resolved.x <= zone.max_x
                               and zone.min_y <= resolved.y <= zone.max_y)
        except (AttributeError, TypeError, ValueError, OverflowError):
            trusted = False
            position_x = position_y = None
        return RobotObservation(
            now=self.clock(), utc=self.utc(), state=state,
            in_cleaning=sample.get('in_cleaning'), in_returning=sample.get('in_returning'),
            summary=getattr(data, 'clean_summary', None), position_trusted=trusted,
            docked=state in ('charging', 'charging_complete'), fresh=fresh,
            sample_id=sampled, sample_utc=sampled,
            outside_danger_zone=outside, position_sample_id=position_sample,
            task_active=is_v1_task_active(
                state=state,
                in_cleaning=sample.get('in_cleaning'),
                in_returning=sample.get('in_returning'),
            ),
            x=position_x, y=position_y,
        )

    def close_allowed(self) -> bool:
        entity_id = self.binding.get('close_permission_entity')
        if not entity_id:
            return True
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == 'on' and not state.attributes.get('restored')

    def supports_stop(self) -> bool:
        cover = self.hass.states.get(self.binding.get('cover_entity_id', ''))
        return cover is not None and bool(cover.attributes.get('supported_features', 0) & 8)

    # -- actions -------------------------------------------------------------

    async def move_door(self, direction: str, operation_id: int | None = None) -> None:
        """Command the cover under our own context so it is not read as external."""
        context = self._make_context()
        self._contexts.append(context.id)
        self._contexts = self._contexts[-64:]
        await self.hass.services.async_call(
            'cover', f'{direction}_cover', {'entity_id': self.binding['cover_entity_id']},
            blocking=True, context=context,
        )

    def _make_context(self) -> Any:
        """Build a Home Assistant context, or a stand-in when running headless.

        Keeping this in one place lets the controller be exercised without Home
        Assistant; a hard import inside ``move_door`` made every headless run fail
        with ``door_command_failed`` before the service was even called.
        """
        try:
            from homeassistant.core import Context
        except ImportError:  # headless tests
            return SimpleNamespace(id=f'ctx-{len(self._contexts)}')
        return Context()

    async def async_control_command(self, command: str, params: Any = None) -> Any:
        """Send the robot commands the controller owns: pause, resume, waypoint."""
        name = command_name(command)
        if name not in ('app_pause', 'app_resume', 'app_goto_target'):
            raise GarageControlError('unsupported_guarded_command')
        if name == 'app_goto_target':
            if (not isinstance(params, (list, tuple)) or len(params) != 2
                    or any(type(value) is not int for value in params)):
                raise GarageControlError('invalid_controlled_exit_target')
        if self._closed:
            raise GarageControlError('control_unloaded')
        if name == 'app_goto_target':
            response = await self.coordinator.properties_api.command.send(name, list(params))
        else:
            self.coordinator.last_resume_command = 'app_resume'
            response = await self.coordinator.properties_api.command.send(name)
        await self.coordinator.async_refresh()
        return response

    # -- lifecycle -----------------------------------------------------------

    async def async_restore(self) -> None:
        """Verify the binding is coherent; there is no saved task to recover."""
        self.entry = self.coordinator.config_entry
        refresh = getattr(self.coordinator, 'async_refresh', None)
        if refresh is not None:
            await refresh()
        self.controller.refresh()
        self._sync_phase_with_door()

    def _sync_phase_with_door(self) -> None:
        """Keep the reported phase honest when no sequence is running.

        The phase describes what the integration is doing. With no clean in flight the
        only thing it can honestly say is what the door looks like: closed, open, or
        unreadable. Without this, a phase chosen at start-up (when the contacts had not
        reported yet) would persist for hours and contradict the door it describes.
        """
        if self.controller.task_in_progress:
            # A sequence owns the phase; it is authoritative about its own progress.
            return
        if self.controller.return_in_progress and not self.controller.return_completed:
            # A return is under way. Reporting 'idle' because the door happens to be
            # shut erases the fact that the robot is still outside waiting to come in,
            # and the phase is what the departure rule and the household read.
            return
        if self.controller.closed_confirmed():
            self.controller.phase = 'idle'
        elif self.controller.open_confirmed():
            self.controller.phase = 'outside'
        elif self.controller.door_blocked_reason() is not None:
            self.controller.phase = 'blocked'

    async def async_start(self) -> None:
        """Bring the binding up. Starts no loop and commands nothing.

        There used to be a one-second background loop here that re-evaluated the door
        against the robot's state. It is what shut a hand-opened door: a poll has no
        notion of a sequence, so "door open, robot docked, nothing happening" looked
        like a reason to close, once a second, all evening.

        Nothing replaces it. The only caller that moves the door is a clean sequence,
        which runs inside ``async_send`` and handles its own mid-task returns and final
        close. A door operated by the household sees no integration activity at all.
        """
        await self.async_restore()
        await self.async_attach()
        self.publish()

    def shutdown(self) -> None:
        """Stop following any clean in progress, and give the door back.

        Releasing ownership is not tidiness. `async_setup_garages` refuses a cover
        that is already in `owners` with `duplicate_binding`, and that map is
        process-wide while a reload builds a brand-new runtime. Leaving the entry
        behind meant any reload -- including a plain `automation.reload`, which
        restarts the whole config entry -- permanently disabled the garage until
        Home Assistant itself restarted: the runtime was never attached, so every
        clean command was refused with `session_recovery_required` and the robot
        only beeped.

        Only the entry that is still *this* runtime is removed. A late second call
        to `shutdown()` must not evict the runtime that replaced us.
        """
        self._closed = True
        if self._sequence_task is not None:
            self._sequence_task.cancel()
            self._sequence_task = None
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        self._listeners.clear()
        owners = getattr(self.hass, 'data', {}).get(REGISTRY_KEY)
        if isinstance(owners, dict):
            cover = self.binding.get('cover_entity_id')
            if cover is not None and owners.get(cover) is self:
                del owners[cover]

    # -- the sequence --------------------------------------------------------

    async def _pause_robot(self) -> None:
        await self.async_control_command('app_pause')

    async def _resume_robot(self) -> None:
        await self.async_control_command('app_resume')

    async def _handle_return(self) -> bool:
        """Pause, open, resume while the robot is driving home."""
        self.controller.refresh()
        if not self.controller.is_returning():
            return False

        async def pause() -> None:
            await self.async_control_command('app_pause')

        async def resume() -> None:
            await self.async_control_command('app_resume')

        try:
            await self.controller.update_returning(pause=pause, resume=resume)
        except GarageControlError as err:
            # Held, not swallowed. The robot is stopped in front of a door that is
            # not confirmed open -- the safe outcome, and one a human has to
            # resolve. Measured 2026-11-12: this branch logged at `info` and the
            # loop carried on for 81 minutes. The robot sat `paused` outside a
            # closed door; `garage_problem` read "off" because `last_error` had
            # been dropped; no event fired; and the blueprint could not notice
            # either, because it waits for the task to END and a paused robot's
            # task never ends.
            #
            # So the hold is announced. `notify` deduplicates on the reason, so a
            # loop that keeps retrying the same failure does not spam.
            _LOGGER.warning(
                'Garage held on return for %s: %s (the robot is stopped and waits '
                'for a human)', self.coordinator.duid, err.reason)
            self.notify(err.reason)
        self.publish()
        return True

    # -- command entry -------------------------------------------------------

    async def async_send(self, command: Any, send: Callable, *, _stage_owner: object | None = None) -> Any:
        """Run one cleaning sequence: open, release the robot, wait, close.

        This is the whole ordered process, not a wrapper around a command:

        1. open the door and confirm it is open, so the robot has a way out;
        2. send the clean command, which releases the robot;
        3. wait for the robot to come home, reopening the door for any mid-task
           recharge visit the robot makes on its own;
        4. close the door behind it and finish.

        Step 1 must precede step 2. Waiting to observe the robot moving would be too
        late: by then it has left the dock, and a pause mid-manoeuvre strands it in the
        cabinet opening.

        Steps 3 and 4 are what give the door its beginning and end. Without them the
        only remaining close rule would be a background poll of the door's position,
        which is what closed a hand-opened door seconds after the household opened it.
        """
        name = command_name(command)
        if name not in _NEW_TASK_COMMANDS | _RESUME_COMMANDS | _DIRECT_COMMANDS | {'app_charge'}:
            raise GarageControlError('unsupported_guarded_command')
        if name in _DIRECT_COMMANDS:
            return await send()
        if self._closed:
            raise GarageControlError('control_unloaded')

        if name == 'app_charge':
            return await self._send_charge(command, send)

        if name not in _NEW_TASK_COMMANDS and name not in _RESUME_COMMANDS:
            # Anything else that is not a clean: no sequence to run.
            return await send()

        exited = False
        if name in _CONTROLLED_EXIT_COMMANDS and self._should_controlled_exit(name):
            await self._run_controlled_exit()
            # The exit already opened the door, confirmed it, drove the robot out
            # and shut it again. Calling `open_door()` next would reopen the door
            # the trip just closed -- measured against the old entry point, where
            # `cover.open_cover` was issued twice and the clean then started with
            # the door open. The robot is past the door now, so it does not need
            # passage; the spec's REQ-39 says exactly this.
            exited = True
        if not exited:
            await self.controller.open_door()
        self.publish()
        try:
            result = await send()
        except Exception:
            # The robot never took the command, so the sequence did not start. Leave
            # the door where it is: closing it here would be an unrequested move.
            self.publish()
            raise
        self.publish()
        # The door work that follows the command belongs to this clean, but it must
        # not block the caller: ``vacuum.clean_area`` has to return promptly, and a
        # clean can run for hours. The sequence continues as one owned task.
        self._sequence_task = asyncio.create_task(self._finish_sequence())
        return result

    async def _send_charge(self, command: Any, send: Callable) -> Any:
        """Send the robot home with the door already open.

        `app_charge` used to be forwarded untouched. That is right when the robot
        is on its dock or already inside, and wrong in exactly one case: a clean is
        running, the robot is beyond the door, and the door has been shut behind it
        by the departure rule. The household presses "return to base" in Home
        Assistant, the robot drives at a closed door.

        So when the robot is working and away from the dock, nothing is sent until
        the door is confirmed open. Pausing first is not about the door: a robot
        already driving home may be metres away and turn back, so it is stopped
        before anything else changes, then released once the way is clear.

        Everything else falls through untouched -- a docked robot, an idle robot,
        a cancelled sequence. Sending the command late would be a different bug
        from sending it with the door shut.
        """
        self.controller.refresh()
        if not self._needs_passage_for_charge():
            return await send()

        _LOGGER.info(
            'Garage: sending %s home from outside; pausing, opening, then releasing',
            self.coordinator.duid)
        try:
            await self._pause_robot()
            self.controller.refresh()
            if self.controller.robot_state() != 'paused':
                # Could not stop it. Opening the door is still the safe move: an
                # open door cannot be driven into.
                _LOGGER.warning(
                    'Garage: could not pause %s before sending it home',
                    self.coordinator.duid)
            await self.controller.open_door()
            self.publish()
            result = await send()
        except GarageControlError:
            self.publish()
            raise
        self.publish()
        return result

    def _needs_passage_for_charge(self) -> bool:
        """Whether the robot has to come in through the door to reach its dock.

        True when the robot is running a task and is not already on the dock. A
        robot inside the cabinet needs no passage; one outside does, whether the
        door is open now or was shut moments ago.
        """
        if self.controller.robot_docked():
            return False
        if not self.controller.task_running():
            return False
        # Outside means the door is between it and the dock.
        return self.controller.robot_outside()

    #: How long a clean gets to prove it started before the sequence gives up on it.
    #: Long enough for a command to reach the robot and for the robot to leave its dock;
    #: short enough that a stale sequence cannot survive into the next hour.
    START_CONFIRMATION_SECONDS = 180

    async def _finish_sequence(self) -> None:
        """Carry one clean through to its end: mid-task returns, then the final close.

        Started by ``async_send`` right after the command is accepted, so this belongs
        to exactly one clean. It ends when the robot is home and the door is shut, or
        when the sequence times out.

        It first waits for proof that the clean actually began. A command can be accepted
        and still never move the robot -- a dropped cloud call, a robot that was busy, a
        segment list the device rejected. Without that proof the sequence would find a
        docked robot with no task, call it "finished", and close a door nobody asked it
        to close. That is the one way this code could move a door uninvited, so it does
        not proceed until the robot has been seen working.
        """
        timeout = float(self.binding.get('sequence_timeout_minutes', 480)) * 60
        deadline = self.clock() + max(1.0, timeout)
        try:
            if not await self._await_clean_started():
                return
            while True:
                if self._closed:
                    return
                self.controller.refresh()
                if await self._handle_return():
                    pass
                elif await self.controller.update_after_departure(
                    pause=self._pause_robot, resume=self._resume_robot,
                ):
                    pass
                self.publish()
                if await self.controller.sequence_finished():
                    return
                if self.clock() >= deadline:
                    _LOGGER.warning(
                        'Garage sequence for %s did not finish within %.0f minutes; '
                        'leaving the door as it is', self.coordinator.duid, timeout / 60)
                    return
                await self._sequence_sleep(1.0)
        except asyncio.CancelledError:
            raise
        except GarageControlError as err:
            _LOGGER.info('Garage sequence for %s stopped: %s', self.coordinator.duid, err.reason)
            # A sequence that dies must say so. Measured 2026-11-12: the return
            # pause was confirmed, the door would not open, the robot was left
            # paused in front of a closed door, and the only record anywhere was
            # this log line -- no event, so `garage_problem` stayed "off" and the
            # household found the robot by accident 81 minutes later.
            #
            # The blueprint's own completion notice could not help either: it
            # waits for the task to end, and the task never ended because the
            # robot was still paused.
            self.notify(err.reason)
            self.publish()
        except Exception:  # noqa: BLE001 - one bad sequence must not escape silently
            _LOGGER.exception('Garage sequence for %s failed', self.coordinator.duid)
            self.notify('sequence_failed')
            self.publish()
        finally:
            self.publish()

    async def _await_clean_started(self) -> bool:
        """Wait until the robot is actually working, or give up on this clean.

        Returns whether the clean is confirmed running. False means it never started, so
        the caller must not treat anything that follows as part of a task -- in
        particular it must not close the door on the strength of it.

        "Working" means an active task while away from the dock. Both halves are needed: a
        docked robot mid-task is recharging and will come back out, while a robot away
        from its dock with no task is returning from something else and is not this clean.
        """
        deadline = self.clock() + self.START_CONFIRMATION_SECONDS
        while True:
            if self._closed:
                return False
            self.controller.refresh()
            if self.controller.task_running() and not self.controller.robot_docked():
                return True
            if self.clock() >= deadline:
                _LOGGER.warning(
                    'Garage sequence for %s: the clean never started within %d s; '
                    'leaving the door as it is', self.coordinator.duid,
                    self.START_CONFIRMATION_SECONDS)
                self.controller.task_in_progress = False
                return False
            await self._sequence_sleep(1.0)

    def _exit_geometry(self) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]] | None:
        """Return (dock, zone_min, zone_max), or None when either is unreadable."""
        zone = self._zone_provider()
        if zone is None:
            return None
        try:
            map_data = self.coordinator.properties_api.map_content.map_data
            charger = getattr(map_data, 'charger', None)
        except (AttributeError, TypeError):
            return None
        if charger is None:
            return None
        dock_x, dock_y = getattr(charger, 'x', None), getattr(charger, 'y', None)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in (dock_x, dock_y)):
            return None
        return (
            (float(dock_x), float(dock_y)),
            (float(zone.min_x), float(zone.min_y)),
            (float(zone.max_x), float(zone.max_y)),
        )

    def _controlled_exit_distance(self) -> int | None:
        """Return the distance to drive past the zone edge.

        A binding saved before 0.12.0 stores a coordinate waypoint instead. It is
        converted here rather than at validation time because the conversion needs
        the danger zone and the charger, and the validator is a pure function. The
        legacy coordinates are never read for anything else.

        Returns None when the binding carries neither a distance nor a convertible
        waypoint, or when the geometry needed to convert is unavailable.
        """
        if 'controlled_exit_distance_mm' in self.binding:
            return int(self.binding['controlled_exit_distance_mm'])
        geometry = self._exit_geometry()
        if geometry is None:
            return None
        raw_x = self.binding.get('controlled_exit_x')
        raw_y = self.binding.get('controlled_exit_y')
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in (raw_x, raw_y)):
            return None
        try:
            converted = legacy_exit_distance(
                waypoint=(float(raw_x), float(raw_y)), dock=geometry[0],
                zone_min=geometry[1], zone_max=geometry[2],
                direction=self.binding.get('controlled_exit_direction', 'auto'),
            )
        except GarageConfigError as err:
            _LOGGER.warning(
                'Garage exit waypoint for %s cannot be converted: %s',
                self.coordinator.duid, err.reason)
            return None
        if converted is None:
            return None
        distance, lateral = converted
        if lateral > CONTROLLED_EXIT_LATERAL_WARN_MM:
            _LOGGER.warning(
                'Garage exit waypoint for %s sits %.0f mm off the exit axis; the '
                'lateral component cannot be represented and is dropped',
                self.coordinator.duid, lateral)
        return distance

    def _controlled_exit_settings(self) -> dict[str, Any]:
        """Resolve the waypoint from direction and distance.

        Raises when it cannot be resolved: driving to an invented point is worse
        than not driving at all, and a clean must not start with the door shut
        because the exit silently did nothing.
        """
        distance = self._controlled_exit_distance()
        geometry = self._exit_geometry()
        if distance is None or geometry is None:
            raise GarageControlError('exit_target_unavailable')
        target = resolve_exit_target(
            dock=geometry[0], zone_min=geometry[1], zone_max=geometry[2],
            direction=self.binding.get('controlled_exit_direction', 'auto'),
            distance_mm=distance,
        )
        if target is None:
            raise GarageControlError('exit_target_unavailable')
        return {
            'x': target[0],
            'y': target[1],
            'direction': self.binding.get('controlled_exit_direction', 'auto'),
            'distance_mm': distance,
            'timeout': float(self.binding['controlled_exit_timeout_seconds']),
        }

    def _should_controlled_exit(self, name: str) -> bool:
        """Whether this clean should be preceded by a straight run out.

        Only a brand-new clean: an active robot is already outside, and a resume
        belongs to a task whose door handling has finished.
        """
        if not self.binding.get('controlled_exit_enabled'):
            return False
        if name not in _CONTROLLED_EXIT_COMMANDS:
            return False
        if self.controller.task_running():
            return False
        if not self.controller.robot_docked():
            return False
        return True

    async def _run_controlled_exit(self) -> None:
        """Drive straight out, shut the door, then let the caller send the clean.

        Failure is terminal for this attempt: a clean must not start inside the
        cabinet with the door shut.

        Every exit ends with the controller in a state somebody can reason about.
        That is not decoration: on 2026-11-08 the arrival check could not succeed,
        the deadline raised, and nothing else ran -- the sequence flag still said a
        clean owned the door, the phase still said ``controlled_exit``,
        ``last_error`` was empty so ``garage_problem`` read "off", and no event was
        fired. The door stayed open for hours with no alarm. So the countdown is
        wrapped: on the way out the door is put in the safest state its evidence
        supports, and the failure is always announced.
        """
        settings = self._controlled_exit_settings()
        target = (settings['x'], settings['y'])
        self.controller.refresh()
        await self.controller.open_door()
        self.controller.phase = 'controlled_exit'
        self.publish()
        try:
            await self.async_control_command('app_goto_target', list(target))
            await self._await_controlled_exit_arrival(target, settings['timeout'])
        except BaseException as err:  # noqa: BLE001 - every exit path must settle
            await self._abandon_controlled_exit(err)
            raise
        await self.controller.close_door()
        self.controller.phase = 'closed_outside'
        self.publish()

    #: Distinct status samples that must agree before the door may move.
    #:
    #: Counted by sample identity, not by loop iteration and not by elapsed time.
    #: ``refresh()`` re-reads the last *cached* sample, and this loop wakes several
    #: times per poll, so an iteration counter would count one reading several
    #: times. A wall-clock settle has the same hole whenever the loop runs faster
    #: than the poll -- which it does. Two *different* polls is the claim worth
    #: making: the robot said "arrived" twice.
    CONTROLLED_EXIT_SETTLE_SAMPLES = 2

    async def _await_controlled_exit_arrival(
        self, target: tuple[float, float], timeout: float,
    ) -> None:
        """Wait until the trip is over and the robot is clear of the zone.

        The distance the operator configured is honoured by the robot's own
        ``app_goto_target``; this loop only decides when to stop waiting and lets
        the caller report where the robot actually ended up. Waiting for the
        coordinate to fall inside a tolerance instead would make a short distance
        meaningless: a 100mm waypoint with a 200mm tolerance counted as arrived the
        moment the robot cleared the zone edge.
        """
        deadline = self.clock() + float(timeout)
        agreeing: set[Any] = set()
        while True:
            self.controller.refresh()
            if self.controller.controlled_exit_arrived(target):
                agreeing.add(self.controller.robot.sample_id)
                if len(agreeing) >= self.CONTROLLED_EXIT_SETTLE_SAMPLES:
                    self._report_arrival_offset(target)
                    return
            else:
                agreeing.clear()
            if self.clock() >= deadline:
                raise GarageControlError('controlled_exit_failed')
            await asyncio.sleep(0.2)

    def _report_arrival_offset(self, target: tuple[float, float]) -> None:
        """Record how far the robot stopped from the waypoint it was given.

        Not a failure: the trip ended and the robot is outside, so the door can
        move. But if it stopped well short -- blocked, or the waypoint was
        unreachable -- saying nothing would let the household believe the
        configured distance was honoured.
        """
        from .garage_config import CONTROLLED_EXIT_TOLERANCE_MM

        offset = self.controller.controlled_exit_arrival_offset(target)
        self.exit_arrival_offset_mm = None if offset is None else round(offset, 1)
        if offset is None:
            _LOGGER.info(
                'Garage exit for %s finished; position unavailable, so the '
                'waypoint could not be verified', self.coordinator.duid)
            return
        if offset > CONTROLLED_EXIT_TOLERANCE_MM:
            _LOGGER.warning(
                'Garage exit for %s finished %.0f mm from the waypoint (tolerance '
                '%.0f mm); the door is still safe to close because the robot is '
                'confirmed outside the danger zone',
                self.coordinator.duid, offset, CONTROLLED_EXIT_TOLERANCE_MM)

    async def _abandon_controlled_exit(self, err: BaseException) -> None:
        """Leave a failed exit in a state the household can see and act on.

        The door goes shut only when the robot is *confirmed* outside and not on
        its dock: then the trip it was opened for is over, and an open door has
        no purpose. When that cannot be shown, the door is left as it is -- open
        is untidy, closing on a robot that may still be in the doorway is damage.
        Either way the flag that made this sequence the door's owner is cleared,
        so a later clean is not refused by work that no longer exists.
        """
        reason = getattr(err, 'reason', None) or 'controlled_exit_failed'
        if not isinstance(err, GarageControlError):
            _LOGGER.exception('Garage controlled exit for %s failed', self.coordinator.duid)
        else:
            _LOGGER.warning(
                'Garage controlled exit for %s stopped: %s (state=%s, outside=%s)',
                self.coordinator.duid, reason, self.controller.robot_state(),
                getattr(self.controller.robot, 'outside_danger_zone', None),
            )
        # The reported failure stays the exit's own, even when the tidy-up below
        # succeeds: `close_door` clears `last_error` on success, which would hide
        # the reason this sequence died and leave `garage_problem` reading "off".
        reported = reason
        try:
            if self.controller.robot_outside():
                await self.controller.close_door()
        except GarageControlError as close_err:
            _LOGGER.warning(
                'Garage controlled exit for %s could not close the door: %s',
                self.coordinator.duid, close_err.reason,
            )
            reported = close_err.reason
        finally:
            # The flag says "a sequence owns this door". This sequence is over, so
            # leaving it set would make every later clean look like a second one.
            self.controller.task_in_progress = False
            self.controller.last_error = reported
            # Report where the door actually is rather than inventing a stuck
            # phase: the fault is carried by `last_error` / `garage_problem`, and
            # `blocked` is reserved for "the door cannot be read", which is a
            # different problem with a different remedy.
            self._sync_phase_with_door()
            self.notify(reported)
            self.publish()


    # -- explicit entries ----------------------------------------------------

    async def async_request_open(self) -> None:
        await self.controller.open_door()
        self.publish()

    async def async_request_close(self) -> None:
        """Close on explicit request, refusing only when the robot is known to be inside.

        The door is the household's, not this integration's, so a request to close it is
        honoured. The one exception is a robot this integration can see sitting in the
        doorway: closing then would be a collision, and the caller did not ask for that.

        Unknown position is not a refusal. Refusing on missing data would turn a sensor
        problem into a door nobody can close, which is the class of fault this integration
        was rewritten to remove.
        """
        self.controller.refresh()
        inside = (
            getattr(self.controller.robot, 'fresh', False)
            and getattr(self.controller.robot, 'position_trusted', False) is True
            and getattr(self.controller.robot, 'outside_danger_zone', None) is False
            and not self.controller.robot_docked()
        )
        if inside:
            _LOGGER.warning(
                'Garage close refused for %s: the robot is inside the danger zone '
                '(state=%s, docked=%s)', self.coordinator.duid,
                self.controller.robot_state(), self.controller.robot_docked())
            raise GarageControlError('robot_in_the_way')
        _LOGGER.info(
            'Garage close requested for %s (robot state=%s, docked=%s, outside=%s)',
            self.coordinator.duid, self.controller.robot_state(),
            self.controller.robot_docked(),
            getattr(self.controller.robot, 'outside_danger_zone', None))
        await self.controller.close_door()
        self.publish()

    async def async_routine(self, send: Callable) -> Any:
        """A server-side routine drives the robot, so open the door first."""
        await self.controller.open_door()
        self.publish()
        try:
            return await send()
        finally:
            self.publish()

    def reset(self) -> None:
        """Re-read reality. Nothing is latched, so there is nothing to release.

        Kept because the service still exists; it is a plain observation refresh,
        not an escape hatch from a hold.
        """
        self.controller.refresh()
        self.controller.phase = 'idle' if self.controller.closed_confirmed() else 'outside'
        self.publish()

    # -- stages (kept for the multi-stage scripts) ---------------------------

    async def async_start_stage(self, stage: dict, send: Callable, *, previous_receipt: dict | None = None) -> dict:
        """Run one stage and wait here until it finishes.

        The old protocol returned a receipt immediately and the script then polled
        the phase sensor for ``running`` -> ``succeeded``. That only worked because
        a task session tracked progress between the two calls; without one there is
        no ``running`` to observe, so the wait would hang until its own timeout.

        Blocking instead is simpler and cannot get stuck: the caller waits for the
        answer, and the answer is the real outcome.
        """
        command = stage.get('command')
        if command not in _CONTROLLED_EXIT_COMMANDS:
            raise GarageControlError('invalid_stage')
        try:
            await self.async_send(command, send)
            await self._await_stage_completion(
                float(stage.get('timeout_minutes',
                                self.binding.get('stage_timeout_minutes', 180))) * 60
            )
            self.stage_result, self.stage_reason = 'succeeded', None
        except Exception as err:  # noqa: BLE001 - reported to the script
            self.stage_result = 'failed'
            self.stage_reason = getattr(err, 'reason', None) or 'stage_failed'
            raise
        finally:
            self.publish()
        return {'runtime_id': 'position-driven', 'session_id': None,
                'interruption_revision': 0,
                'result': self.stage_result, 'reason': self.stage_reason}

    async def _await_stage_completion(self, timeout_seconds: float) -> None:
        """Block until the task this stage started is over.

        "Over" means the robot reports no active task *and* is back on its dock.
        Both are needed: a robot between passes sits docked mid-task with
        ``task_active`` still true, and a robot past the zone with the task over is
        still on its way home.

        A timeout is deliberately not success. A stage that never reports
        completion is one nobody can vouch for, and the script must hear that
        rather than move on to the next room.
        """
        deadline = self.clock() + max(1.0, timeout_seconds)
        while True:
            if self._closed:
                raise GarageControlError('control_unloaded')
            self.controller.refresh()
            if not self.controller.task_running() and self.controller.robot_docked():
                return
            if self.clock() >= deadline:
                raise GarageControlError('stage_timeout')
            await asyncio.sleep(1.0)

    def stage_attributes(self) -> dict[str, Any]:
        return {'stage_result': self.stage_result, 'stage_reason': self.stage_reason}

    def exit_target_attributes(self) -> dict[str, Any]:
        """Expose the resolved waypoint so a surprise trip can be explained.

        The waypoint is now derived rather than stored, so "where will it go?"
        cannot be answered by reading the config. These four values answer it
        without guessing, and the legacy pair stays visible for a binding that was
        migrated: it is what the old behaviour was, and is no longer read.
        """
        if not self.binding.get('controlled_exit_enabled'):
            return {}
        attributes: dict[str, Any] = {}
        geometry = self._exit_geometry()
        requested = self.binding.get('controlled_exit_direction', 'auto')
        if geometry is not None:
            axis = resolve_exit_direction(
                dock=geometry[0], zone_min=geometry[1], zone_max=geometry[2],
                requested=requested,
            )
            attributes['controlled_exit_direction'] = axis or requested
        else:
            attributes['controlled_exit_direction'] = requested
        try:
            settings = self._controlled_exit_settings()
        except GarageControlError as err:
            attributes['controlled_exit_target_unavailable'] = err.reason
        else:
            attributes['controlled_exit_distance_mm'] = settings['distance_mm']
            attributes['controlled_exit_target'] = [settings['x'], settings['y']]
        if self.exit_arrival_offset_mm is not None:
            attributes['controlled_exit_arrival_offset_mm'] = self.exit_arrival_offset_mm
        legacy = {
            key: self.binding[key]
            for key in ('controlled_exit_x', 'controlled_exit_y')
            if key in self.binding
        }
        if legacy:
            attributes['controlled_exit_legacy_xy'] = legacy
        return attributes

    # -- events and listeners ------------------------------------------------

    def notify(self, reason: str | None) -> None:
        """Publish a state-change hint. Never used to demand operator action."""
        if reason == self._last_issue:
            return
        self._last_issue = reason
        self.hass.bus.async_fire(EVENT_GARAGE_ISSUE, {
            'reason': reason,
            'phase': self.controller.phase,
            'previous_phase': None,
            'cover_entity_id': self.binding.get('cover_entity_id'),
            'vacuum_entity_id': getattr(self.coordinator, 'garage_vacuum_entity_id', None),
            'active': reason is not None,
        })

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def publish(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def _service_event(self, event: Any) -> None:
        """Observe cover commands we did not issue.

        Informational only: an external command is logged, never latched. The
        household may close the door by hand at any time without locking the next
        schedule out.
        """
        data = event.data
        if data.get('domain') != 'cover' or getattr(event.context, 'id', None) in self._contexts:
            return
        target = data.get('service_data', {}).get('entity_id', [])
        if isinstance(target, str):
            target = [target]
        if target and self.binding.get('cover_entity_id') not in target and 'all' not in target:
            return
        _LOGGER.info('Garage observed an external cover command: %s', data.get('service'))

    def schedule(self, *_: Any) -> None:
        """Ask for a tick soon; the loop is the only writer."""
        return

    # -- wiring --------------------------------------------------------------

    async def async_attach(self) -> None:
        """Subscribe to what changes the door's facts, and nothing else.

        A clean sequence runs inside ``async_send`` and owns both its door moves, so
        nothing needs to *notice* the door changing in order to act on it. What does need
        to notice is the diagnostic: Home Assistant stores what an entity reported the
        last time it wrote its state, so without a write on door changes the entity would
        describe the door as it was at start-up forever.

        This is a state listener, not a timer, and it only recomputes and re-publishes.
        It never commands anything -- the difference matters: a listener that reacts to
        the door being open is how a monitor turns into a hardware actor.
        """
        from homeassistant.core import callback
        from homeassistant.helpers.event import async_track_state_change_event

        @callback
        def on_service(event: Any) -> None:
            self._service_event(event)

        @callback
        def on_door_change(event: Any) -> None:
            # Recompute from the current states and let the entities write themselves.
            self.controller.refresh()
            self._sync_phase_with_door()
            self.publish()

        watched = [entity for entity in (
            self.binding.get('cover_entity_id'),
            self.binding.get('open_contact'),
            self.binding.get('close_contact'),
            self.binding.get('close_permission_entity'),
        ) if entity]

        if watched:
            self._unsubscribers.append(
                async_track_state_change_event(self.hass, watched, on_door_change))
        self._unsubscribers.append(self.hass.bus.async_listen('call_service', on_service))

async def async_setup_garages(hass: Any, entry: Any) -> None:
    """Activate only explicitly enabled, validated per-robot bindings."""
    from .safe_zone_store import get_safe_zone_store
    await get_safe_zone_store(hass).async_load()
    owners = hass.data.setdefault(REGISTRY_KEY, {})
    entries = []
    for candidate in hass.config_entries.async_entries('roborock_plus'):
        runtime = getattr(candidate, 'runtime_data', None)
        duids = [c.duid for c in getattr(runtime, 'v1', [])] if runtime is not None else ()
        entries.append((candidate.entry_id, candidate.options, duids))

    for coordinator in entry.runtime_data.v1:
        coordinator.garage_runtime = None
        coordinator.garage_setup_error = None
        if not configured(coordinator):
            continue
        runtime = None
        try:
            binding = resolve_binding(entry.options, coordinator.duid)
            if binding is None:
                raise GarageControlError('missing_safety_configuration')
            validate_binding_ownership(
                binding, duid=coordinator.duid, entry_id=entry.entry_id, entries=entries)
            if binding['cover_entity_id'] in owners:
                raise GarageControlError('duplicate_binding')
            runtime = GarageRuntime(hass, coordinator, binding)
            owners[binding['cover_entity_id']] = runtime
            coordinator.garage_runtime = runtime
            entry.async_on_unload(runtime.shutdown)
            await runtime.async_start()
        except Exception as err:  # noqa: BLE001 - surfaced on the entity, not fatal
            reason = getattr(err, 'reason', None) or 'garage_setup_failed'
            coordinator.garage_setup_error = reason
            _LOGGER.exception('Garage binding rejected for %s: %s', coordinator.duid, reason)
