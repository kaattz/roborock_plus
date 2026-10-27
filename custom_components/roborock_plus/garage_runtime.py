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
from .garage_config import resolve_binding, validate_binding_ownership
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
    """One coherent robot reading. Physical facts only; no task identity."""

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
        self._update_task: asyncio.Task | None = None
        self._closed = False
        self._last_issue: str | None = None
        self.stage_result: str | None = None
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
            if trusted and fresh and position_sample is not None:
                outside = not (zone.min_x <= resolved.x <= zone.max_x
                               and zone.min_y <= resolved.y <= zone.max_y)
        except (AttributeError, TypeError, ValueError, OverflowError):
            trusted = False
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
        self.controller.phase = 'idle' if self.controller.closed_confirmed() else 'outside'

    async def async_start(self) -> None:
        await self.async_restore()
        await self.async_attach()
        self._update_task = asyncio.create_task(self._run_loop())
        self.publish()

    async def _run_loop(self) -> None:
        while not self._closed:
            try:
                await self.async_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
                _LOGGER.exception('Garage tick failed for %s', self.coordinator.duid)
            await asyncio.sleep(1.0)

    def shutdown(self) -> None:
        self._closed = True
        if self._update_task is not None:
            self._update_task.cancel()
            self._update_task = None
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        self._listeners.clear()

    # -- the tick ------------------------------------------------------------

    async def async_tick(self) -> None:
        """Apply the reactive rules. Opening is never decided here."""
        if self._closed:
            return
        self.controller.refresh()
        if await self._handle_return():
            return
        if await self.controller.update_after_departure(
            pause=self._pause_robot, resume=self._resume_robot,
        ):
            self.publish()
            return
        await self.controller.update_idle()
        self.publish()

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
            _LOGGER.info('Garage held on return for %s: %s', self.coordinator.duid, err.reason)
        self.publish()
        return True

    # -- command entry -------------------------------------------------------

    async def async_send(self, command: Any, send: Callable, *, _stage_owner: object | None = None) -> Any:
        """Run a robot command, opening the door first when it begins cleaning.

        The door is opened and *confirmed* before ``send`` runs: waiting to
        observe the robot moving would be too late, because by then it has left
        the dock and a pause mid-manoeuvre strands it in the cabinet opening.
        """
        name = command_name(command)
        if name not in _NEW_TASK_COMMANDS | _RESUME_COMMANDS | _DIRECT_COMMANDS | {'app_charge'}:
            raise GarageControlError('unsupported_guarded_command')
        if name in _DIRECT_COMMANDS:
            return await send()
        if self._closed:
            raise GarageControlError('control_unloaded')
        if name in _CONTROLLED_EXIT_COMMANDS and self._should_controlled_exit(name):
            await self._run_controlled_exit()
        if name in _NEW_TASK_COMMANDS or name in _RESUME_COMMANDS:
            await self.controller.open_door()
            self.publish()
        result = await send()
        self.publish()
        return result

    def _controlled_exit_settings(self) -> dict[str, Any]:
        return {
            'x': int(self.binding['controlled_exit_x']),
            'y': int(self.binding['controlled_exit_y']),
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
        """
        settings = self._controlled_exit_settings()
        target = (settings['x'], settings['y'])
        self.controller.refresh()
        await self.controller.open_door()
        self.controller.phase = 'controlled_exit'
        self.publish()
        await self.async_control_command('app_goto_target', list(target))
        deadline = self.clock() + settings['timeout']
        while True:
            self.controller.refresh()
            position = getattr(self.controller.robot, 'position', None)
            x = getattr(position, 'x', None)
            y = getattr(position, 'y', None)
            if x is not None and y is not None and math.hypot(x - target[0], y - target[1]) <= 400:
                break
            if self.clock() >= deadline:
                raise GarageControlError('controlled_exit_failed')
            await asyncio.sleep(0.2)
        await self.controller.close_door()
        self.controller.phase = 'closed_outside'
        self.publish()

    # -- explicit entries ----------------------------------------------------

    async def async_request_open(self) -> None:
        await self.controller.open_door()
        self.publish()

    async def async_request_close(self) -> None:
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
        """Run one explicit stage and report its outcome.

        The old receipts carried interruption authority, which needed a task
        session to mean anything. Without one they would be theatre, so this
        reports the stage result and lets the script decide.
        """
        command = stage.get('command')
        if command not in _CONTROLLED_EXIT_COMMANDS:
            raise GarageControlError('invalid_stage')
        try:
            await self.async_send(command, send)
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

    def stage_attributes(self) -> dict[str, Any]:
        return {'stage_result': self.stage_result, 'stage_reason': self.stage_reason}

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
        """Subscribe to the Home Assistant events this runtime cares about."""
        from homeassistant.core import callback
        from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

        entities = [entity for entity in (
            self.binding.get('cover_entity_id'),
            self.binding.get('open_contact'),
            self.binding.get('close_contact'),
            self.binding.get('close_permission_entity'),
        ) if entity]

        @callback
        def on_state(event: Any) -> None:
            self.schedule()

        @callback
        def on_service(event: Any) -> None:
            self._service_event(event)

        self._unsubscribers.extend([
            async_track_state_change_event(self.hass, entities, on_state),
            self.hass.bus.async_listen('call_service', on_service),
            async_track_time_interval(self.hass, on_state, timedelta(seconds=30)),
        ])

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
