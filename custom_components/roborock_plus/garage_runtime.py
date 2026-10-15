"""HA boundary for the opt-in, single-owner garage workflow."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
import math
import time
from typing import Any

from .door_evidence import DoorEvidence
from .garage_config import resolve_binding, validate_binding_ownership
from .garage_commands import SAFE_DIRECT_COMMANDS
from .garage_control import GarageController, GarageControlError
from .garage_session_store import GarageSessionStore
from .garage_task import GarageTaskSession, GarageTaskError, RobotObservation

REGISTRY_KEY = 'roborock_plus_garage_owners'
STORE_KEY = 'roborock_plus_garage_session_store'
EVENT_GARAGE_ISSUE = 'roborock_plus_garage_issue'
INITIALIZED_KEY = 'garage_initialized_duids'
_LOGGER = logging.getLogger(__name__)
_NEW_TASK_COMMANDS = frozenset({
    'app_start', 'app_segment_clean', 'app_zoned_clean', 'app_spot', 'app_goto_target',
})
_RESUME_COMMANDS = frozenset({'app_resume', 'app_resume_build_map'})
_DIRECT_COMMANDS = SAFE_DIRECT_COMMANDS
_MOVING_STATES = frozenset({
    'cleaning', 'spot_cleaning', 'zoned_cleaning', 'segment_cleaning', 'starting',
    'going_to_target', 'docking', 'returning_home', 'going_to_wash_the_mop',
    'washing_the_mop', 'washing_the_mop_2', 'attaching_the_mop', 'detaching_the_mop',
})


def _name(value: Any) -> str:
    return str(getattr(value, 'name', value))


def command_name(command: Any) -> str:
    return str(getattr(command, 'value', command)).lower()


def configured(coordinator: Any) -> bool:
    options = coordinator.config_entry.options
    bindings = options.get('garage_bindings', {})
    return isinstance(bindings, Mapping) and isinstance(bindings.get(coordinator.duid), Mapping) and bindings[coordinator.duid].get('enabled') is True


def check_legacy_ownership(hass: Any, options: Mapping[str, Any], duid: str) -> None:
    """A second robot cannot use the legacy global option to touch an owned door."""
    if not options.get('garage_guard_enabled'):
        return
    owner = getattr(hass, 'data', {}).get(REGISTRY_KEY, {}).get(options.get('garage_door_entity_id'))
    if owner is not None and owner.coordinator.duid != duid:
        raise GarageControlError('duplicate_binding')


class GarageRuntime:
    """Adapt actual HA observations to pure evidence and task state machines."""

    def __init__(
        self, hass: Any, coordinator: Any, binding: Mapping[str, Any], *, store: Any,
        zone_provider: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        utc: Callable[[], float] = time.time,
    ) -> None:
        self.hass, self.coordinator = hass, coordinator
        self.entry = coordinator.config_entry
        self.binding = dict(binding)
        self.store = store
        self.clock, self.utc = clock, utc
        self._zone_provider = zone_provider or self._zone
        self.evidence = DoorEvidence(self.binding)
        self.session = GarageTaskSession(self.binding['dock_settle_seconds'])
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribers: list[Callable[[], None]] = []
        self._contexts: list[str] = []
        self._last_issue: tuple[str | None, str] | None = None
        self._last_payload: dict[str, Any] | None = None
        self._first_install = coordinator.duid not in self.entry.options.get(INITIALIZED_KEY, [])
        self._restored = False
        self._closed = False
        self._update_task: asyncio.Task | None = None
        self._dirty = False
        self.controller = GarageController(
            self.binding, evidence=self.evidence, session=self.session,
            move_door=self.move_door, door_observation=self.door_observation,
            robot_observation=self.robot_observation, close_allowed=self.close_allowed,
            persist=self.persist, notify=self.notify, supports_stop=self.supports_stop,
            clock=clock,
        )

    def _zone(self) -> Any:
        from .safe_zone_store import get_safe_zone_store
        stored = get_safe_zone_store(self.hass).get_loaded(self.coordinator.duid)
        return None if stored is None else stored.zone

    def door_observation(self) -> dict[str, Any]:
        cover = self.hass.states.get(self.binding['cover_entity_id'])
        attrs = {} if cover is None else cover.attributes
        own_close = getattr(getattr(self, 'controller', None), 'phase', None) == 'closing'
        available = (self.binding.get('handover_confirmed') is True and cover is not None
                     and cover.state not in ('unknown', 'unavailable') and (cover.state != 'closing' or own_close))
        restored = attrs.get('restored') is True
        values: dict[str, Any] = dict(cover_available=available, position=attrs.get('current_position'), restored=restored)
        for direction in ('open', 'close'):
            contact = self.hass.states.get(self.binding.get(f'{direction}_contact', ''))
            values[f'{direction}_contact'] = None if contact is None or contact.attributes.get('restored') else contact.state
        return values

    def robot_observation(self) -> RobotObservation:
        c = self.coordinator
        data = getattr(c, 'data', None)
        sample = getattr(c, 'garage_status_sample', None) or {}
        stamp = sample.get('sampled_at')
        sampled = stamp.timestamp() if isinstance(stamp, datetime) else None
        interval = getattr(c, 'update_interval', timedelta(seconds=60))
        max_age = max(1.0, interval.total_seconds() * 2) if isinstance(interval, timedelta) else 120.0
        fresh = bool(getattr(c, 'last_update_success', False) and sampled is not None and 0 <= self.utc()-sampled <= max_age)
        state = str(sample.get('state', 'unknown'))
        trusted = False
        try:
            resolved = c.resolve_vacuum_position()
            zone = self._zone_provider()
            map_data = getattr(c.properties_api.map_content, 'map_data', None)
            charger = getattr(map_data, 'charger', None)
            numbers = (resolved.x, resolved.y, getattr(charger, 'x', None), getattr(charger, 'y', None))
            valid = all(isinstance(n, (int,float)) and not isinstance(n,bool) and math.isfinite(n) for n in numbers)
            trusted = bool(resolved.trusted and valid and zone is not None and
                           zone.min_x <= charger.x <= zone.max_x and zone.min_y <= charger.y <= zone.max_y)
            if state in ('charging', 'charging_complete'):
                trusted = trusted and math.hypot(resolved.x-charger.x, resolved.y-charger.y) <= 1000
        except (AttributeError, TypeError, ValueError, OverflowError):
            trusted = False
        return RobotObservation(
            now=self.clock(), utc=self.utc(), state=state,
            in_cleaning=sample.get('in_cleaning'), in_returning=sample.get('in_returning'),
            summary=getattr(data,'clean_summary',None), position_trusted=trusted,
            docked=state in ('charging','charging_complete'), fresh=fresh, sample_id=sampled, sample_utc=sampled,
        )

    def close_allowed(self) -> bool:
        entity_id = self.binding.get('close_permission_entity')
        if not entity_id:
            return True
        state = self.hass.states.get(entity_id)
        return state is not None and state.state == 'on' and not state.attributes.get('restored')

    def supports_stop(self) -> bool:
        cover = self.hass.states.get(self.binding['cover_entity_id'])
        return cover is not None and bool(cover.attributes.get('supported_features',0) & 8)

    async def move_door(self, direction: str, operation_id: int) -> None:
        from homeassistant.core import Context
        context = Context()
        self._contexts.append(context.id)
        self._contexts = self._contexts[-64:]
        await self.hass.services.async_call(
            'cover', f'{direction}_cover', {'entity_id':self.binding['cover_entity_id']},
            blocking=True, context=context,
        )

    async def persist(self) -> None:
        payload = self.session.to_dict()
        if payload != self._last_payload:
            await self.store.async_save(self.coordinator.duid,payload)
            self._last_payload = deepcopy(payload)

    async def async_restore(self) -> None:
        try:
            await self.store.async_load()
            payload = self.store.get(self.coordinator.duid)
            if payload is None:
                if not self._first_install:
                    raise GarageTaskError('session_recovery_required')
            else:
                self.session.restore(payload)
                self._last_payload = deepcopy(payload)
                self._first_install = False
                self._mark_initialized()
                if self.session.active or not self.session.known_idle:
                    self.controller.fault('session_recovery_required')
        except Exception:
            self.controller.fault('session_recovery_required')
            _LOGGER.exception('Cannot restore garage session for %s', self.coordinator.duid)
        finally:
            self._restored = True

    def _mark_initialized(self) -> None:
        """Record that missing journal data can no longer mean a new install."""
        initialized = set(self.entry.options.get(INITIALIZED_KEY, []))
        if self.coordinator.duid in initialized:
            return
        options = deepcopy(dict(self.entry.options))
        initialized.add(self.coordinator.duid)
        options[INITIALIZED_KEY] = sorted(initialized)
        self.hass.config_entries.async_update_entry(self.entry, options=options)

    async def async_tick(self) -> None:
        if self._closed or not self._restored:
            return
        try:
            self.controller.refresh()
            if self._first_install and not self.controller.reason:
                try:
                    self.session.initialize_idle()
                except GarageTaskError:
                    self.publish()
                    return
                self._mark_initialized()
                await self.persist()
                self._first_install = False
            if self.controller.phase == 'recovering' and self.session.can_close:
                self.controller.phase = 'idle'
            await self.controller.async_update()
            await self.persist()
        except Exception as err:
            self.controller.fault(getattr(err,'reason',None) or 'session_recovery_required')
            _LOGGER.exception('Garage observation failed for %s', self.coordinator.duid)
        self.publish()

    async def async_send(self, command: Any, send: Callable) -> Any:
        name = command_name(command)
        if name not in _NEW_TASK_COMMANDS | _RESUME_COMMANDS | _DIRECT_COMMANDS | {'app_charge'}:
            raise GarageControlError('unsupported_guarded_command')
        if name in _DIRECT_COMMANDS:
            result = await send()
            if name == 'app_stop' and self.session.active:
                self.session.request_cancel()
                await self.persist()
            self.schedule()
            return result
        if not self._restored or self._first_install or self.controller.phase == 'recovering':
            raise GarageControlError('session_recovery_required')
        if name in _NEW_TASK_COMMANDS or (name=='app_charge' and not self.session.active):
            # Read-only pre-send baseline, without increasing steady-state polling.
            try:
                await self.coordinator.properties_api.clean_summary.refresh()
            except Exception:
                self.controller.fault('task_end_unconfirmed')
                raise
        result = await self.controller.async_execute(
            name, send, new_task=name in _NEW_TASK_COMMANDS or (name=='app_charge' and not self.session.active),
            resume=name in _RESUME_COMMANDS,
        )
        self.schedule()
        self.publish()
        return result

    async def async_routine(self, send: Callable) -> Any:
        return await self.async_send('app_start',send)

    async def async_request_close(self) -> None:
        if self._first_install:
            raise GarageControlError('session_recovery_required')
        await self.controller.async_request_close()
        self.publish()

    def reset(self) -> None:
        if self._first_install:
            raise GarageControlError('session_recovery_required')
        self.controller.reset()
        if self.session.active and self.session.started:
            # The V1 entry may lose its volatile pause fallback on restart.
            # Reconciliation restores context only; the user must still request resume.
            self.coordinator.last_resume_command = 'app_resume'
        self.publish()

    def notify(self, reason: str | None) -> None:
        key = None if reason is None else (self.session.session_id,reason)
        if key == self._last_issue:
            return
        old = self._last_issue
        self._last_issue = key
        for issue, active in ((old,False),(key,True)):
            if issue is None:
                continue
            self.hass.bus.async_fire(EVENT_GARAGE_ISSUE,{
                'entry_id':self.entry.entry_id,'duid':self.coordinator.duid,
                'vacuum_entity_id':getattr(self.coordinator,'garage_vacuum_entity_id',None),
                'cover_entity_id':self.binding['cover_entity_id'],'session_id':issue[0],
                'phase':self.controller.phase,'reason':issue[1],'severity':'error','active':active,
            })
        self.publish()

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        def remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)
        return remove

    def publish(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def _service_event(self, event: Any) -> None:
        data = event.data
        if data.get('domain')!='cover' or getattr(event.context,'id',None) in self._contexts:
            return
        service = data.get('service','')
        if service not in ('open_cover','close_cover','stop_cover','set_cover_position','toggle'):
            return
        target = data.get('service_data',{}).get('entity_id',[])
        if isinstance(target,str):
            target=[target]
        # Unknown area/device targets are handled conservatively; their resulting state is also observed.
        seen: set[str] = set()
        def contains_door(entity_id: str) -> bool:
            if entity_id in (self.binding['cover_entity_id'], 'all'):
                return True
            if entity_id in seen:
                return False
            seen.add(entity_id)
            state = self.hass.states.get(entity_id)
            members = [] if state is None else state.attributes.get('entity_id', [])
            if isinstance(members, str):
                members = [members]
            return isinstance(members, list) and any(contains_door(member) for member in members)
        if target and not any(contains_door(entity_id) for entity_id in target):
            return
        direction = 'open' if service=='open_cover' else 'close' if service=='close_cover' else 'stop'
        self.controller.external_command(direction)
        self.publish()

    def _state_event(self, event: Any) -> None:
        relevant={self.binding.get(k) for k in ('cover_entity_id','open_contact','close_contact','close_permission_entity')}
        if event.data.get('entity_id') not in relevant:
            return
        new_state = event.data.get('new_state')
        if (event.data.get('entity_id') == self.binding['cover_entity_id']
            and getattr(new_state, 'state', None) == 'closing'
            and getattr(getattr(event, 'context', None), 'id', None) not in self._contexts
            and self.controller.phase != 'closing'):
            self.controller.external_command('close')
            self.publish()
        # An uncommanded loss of the open endpoint must not silently reopen against a person.
        if self.session.active and self.controller.phase in ('task_open','returning','docked_intermission'):
            self.controller.refresh()
            if self.controller.passage_allowed is not True:
                self.controller.fault('manual_override',manual=True)
        self.schedule()

    def _registry_event(self, event: Any) -> None:
        """Follow registry identities without replaying door/robot operations.

        HA 2026.9.4 emits update events with the new ``entity_id`` and a
        top-level ``old_entity_id``. Removal revokes the existing handover so
        an unrelated entity reusing the old ID cannot inherit permission.
        Normal options reload reconciles the existing session again.
        """
        action = event.data.get('action')
        if self._closed or action not in ('update', 'remove'):
            return
        removed = action == 'remove'
        old = event.data.get('entity_id') if removed else event.data.get('old_entity_id')
        new = None if removed else event.data.get('entity_id')
        if not isinstance(old, str) or (not removed and (not isinstance(new, str) or old == new)):
            return
        reference_keys = ('cover_entity_id', 'open_contact', 'close_contact', 'close_permission_entity')
        fields = tuple(
            key for key in reference_keys if self.binding.get(key) == old
        )
        if not fields:
            return
        owners = self.hass.data.get(REGISTRY_KEY, {})
        if not removed and 'cover_entity_id' in fields:
            owner = owners.get(new)
            if owner is not None and owner is not self:
                self.controller.fault('duplicate_binding')
                self.publish()
                return
            if owners.get(old) is self:
                owners.pop(old)
                owners[new] = self
        if removed:
            # Keep enabled=True so every movement entry stays on the native
            # blocked path instead of falling back to an old global guard.
            self.binding['handover_confirmed'] = False
        else:
            for key in fields:
                self.binding[key] = new
        # Rebuild through the evidence constructor rather than modifying its
        # private configuration or preserving a pre-rename timing estimate.
        self.evidence = DoorEvidence(self.binding)
        self.controller.binding = self.binding
        self.controller.evidence = self.evidence
        self.controller.state = None
        self.controller.fault('feedback_unavailable' if removed else 'session_recovery_required')
        options = deepcopy(dict(self.entry.options))
        bindings = options.get('garage_bindings', {})
        changed = False
        if isinstance(bindings, Mapping):
            updated = dict(bindings)
            # Update shared references in one options transaction. A normal
            # reload may cancel sibling listeners before their next callback.
            for duid, saved in bindings.items():
                if not isinstance(saved, Mapping):
                    continue
                matches = tuple(key for key in reference_keys if saved.get(key) == old)
                if not matches:
                    continue
                saved = dict(saved)
                if removed:
                    saved['handover_confirmed'] = False
                else:
                    for key in matches:
                        saved[key] = new
                updated[duid] = saved
                changed = True
            if changed:
                options['garage_bindings'] = updated
                self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.publish()

    def schedule(self, *_: Any) -> None:
        if self._closed:
            return
        self._dirty=True
        if self._update_task is not None and not self._update_task.done():
            return
        async def update() -> None:
            while self._dirty and not self._closed:
                self._dirty=False
                await self.async_tick()
        self._update_task=self.hass.async_create_task(update(),name='roborock_plus_garage_update')

    async def async_start(self) -> None:
        from homeassistant.core import callback
        from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
        from homeassistant.helpers.event import async_track_time_interval
        await self.async_restore()

        @callback
        def service_event(event: Any) -> None:
            self._service_event(event)

        @callback
        def state_event(event: Any) -> None:
            self._state_event(event)

        @callback
        def registry_event(event: Any) -> None:
            self._registry_event(event)

        @callback
        def schedule(*args: Any) -> None:
            self.schedule(*args)

        self._unsubscribers.extend([
            self.hass.bus.async_listen('call_service',service_event),
            self.hass.bus.async_listen('state_changed',state_event),
            self.hass.bus.async_listen(EVENT_ENTITY_REGISTRY_UPDATED,registry_event),
            self.coordinator.async_add_listener(schedule),
            async_track_time_interval(self.hass,schedule,timedelta(seconds=1)),
        ])
        await self.async_tick()

    def shutdown(self) -> None:
        self._closed=True
        self.controller.shutdown()
        for remove in self._unsubscribers:
            remove()
        self._unsubscribers.clear()
        if self._update_task is not None:
            self._update_task.cancel()
        owners=self.hass.data.get(REGISTRY_KEY,{})
        if owners.get(self.binding['cover_entity_id']) is self:
            owners.pop(self.binding['cover_entity_id'])


async def async_setup_garages(hass: Any, entry: Any) -> None:
    """Activate only explicitly enabled, validated per-robot bindings."""
    from .safe_zone_store import get_safe_zone_store
    await get_safe_zone_store(hass).async_load()
    owners=hass.data.setdefault(REGISTRY_KEY,{})
    entries=[]
    for candidate in hass.config_entries.async_entries('roborock_plus'):
        runtime=getattr(candidate,'runtime_data',None)
        duids=[c.duid for c in getattr(runtime,'v1',[])] if runtime is not None else ()
        entries.append((candidate.entry_id,candidate.options,duids))
    for coordinator in entry.runtime_data.v1:
        coordinator.garage_runtime=None
        coordinator.garage_setup_error=None
        if not configured(coordinator):
            continue
        runtime=None
        try:
            binding=resolve_binding(entry.options,coordinator.duid)
            if binding is None:
                raise GarageControlError('missing_safety_configuration')
            validate_binding_ownership(binding,duid=coordinator.duid,entry_id=entry.entry_id,entries=entries)
            if binding['cover_entity_id'] in owners:
                raise GarageControlError('duplicate_binding')
            store=hass.data.get(STORE_KEY)
            if store is None:
                store=hass.data[STORE_KEY]=GarageSessionStore(hass)
            runtime=GarageRuntime(hass,coordinator,binding,store=store)
            owners[binding['cover_entity_id']]=runtime
            coordinator.garage_runtime=runtime
            entry.async_on_unload(runtime.shutdown)
            await runtime.async_start()
        except Exception as err:
            reason=getattr(err,'reason',None) or 'session_recovery_required'
            coordinator.garage_setup_error=reason
            if runtime is not None:
                runtime.controller.fault(reason)
            _LOGGER.exception('Garage binding rejected for %s: %s',coordinator.duid,reason)
