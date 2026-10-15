"""Runtime adapters are exercised against in-memory HA boundaries only."""

import asyncio
from datetime import datetime, timezone, timedelta
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest


ROOT = Path(__file__).resolve().parents[1] / 'custom_components/roborock_plus'


def runtime_module():
    assert (ROOT / 'garage_runtime.py').exists(), 'HA garage runtime is missing'
    package = ModuleType('_garage_runtime_tests')
    package.__path__ = [str(ROOT)]
    sys.modules.setdefault(package.__name__, package)
    return importlib.import_module(package.__name__ + '.garage_runtime')


def binding():
    return dict(enabled=True, cover_entity_id='cover.door', task_door_mode='hold_open',
                open_source='contact', close_source='contact', open_contact='binary_sensor.open',
                close_contact='binary_sensor.closed', open_contact_state='on', close_contact_state='on',
                open_position=95, close_position=5, open_travel_seconds=1, close_travel_seconds=1,
                travel_margin_seconds=0, contact_settle_seconds=0, position_settle_seconds=0,
                operation_timeout_seconds=3, dock_settle_seconds=1,
                allow_estimated=False, handover_confirmed=True)


class MemoryStore:
    def __init__(self):
        self.data = {}
    async def async_load(self):
        pass
    def get(self, duid):
        return self.data.get(duid)
    async def async_save(self, duid, payload):
        self.data[duid] = payload


class Boundaries:
    def __init__(self, initialized=False):
        self.now = 0.0
        self.utc = 1000.0
        self.events = []
        self.cover = NS(state='open', attributes={'current_position': 100, 'supported_features': 11})
        self.states = {
            'cover.door': self.cover,
            'binary_sensor.open': NS(state='on', attributes={}),
            'binary_sensor.closed': NS(state='off', attributes={}),
        }
        options = {'garage_control_version': 1, 'garage_bindings': {'robot': binding()}}
        if initialized:
            options['garage_initialized_duids'] = ['robot']
        self.entry = NS(entry_id='entry', options=options)
        self.status = NS(state=NS(name='charging'), in_cleaning=0, in_returning=0)
        self.summary = NS(records=[1], clean_count=1,
                          last_clean_record=NS(begin=500, end=600, complete=1))
        self.coord = NS(duid='robot', config_entry=self.entry, last_update_success=True,
                        update_interval=timedelta(seconds=1),
                        data=NS(status=self.status, clean_summary=self.summary),
                        properties_api=NS(map_content=NS(map_data=NS(charger=NS(x=5,y=5)))),
                        _last_update_success_time=datetime.fromtimestamp(self.utc, timezone.utc),
                        resolve_vacuum_position=lambda: NS(trusted=True,x=5,y=5,from_dock=True))
        self.coord.garage_status_sample = {'sampled_at': self.coord._last_update_success_time,
                                          'state': 'charging', 'in_cleaning': 0, 'in_returning': 0}
        self.hass = NS(data={}, states=NS(get=self.states.get),
                       bus=NS(async_fire=lambda name,data: self.events.append((name,data))),
                       config_entries=NS(async_update_entry=self.update_options))
        self.store = MemoryStore()

    def update_options(self, entry, *, options):
        entry.options = options

    def create(self):
        self.runtime = runtime_module().GarageRuntime(
            self.hass, self.coord, binding(), store=self.store,
            zone_provider=lambda: NS(min_x=0,max_x=10,min_y=0,max_y=10),
            clock=lambda: self.now, utc=lambda: self.utc)
        return self.runtime

    def advance(self):
        self.now += 1.0
        self.utc += 1.0
        self.coord._last_update_success_time = datetime.fromtimestamp(self.utc, timezone.utc)
        self.coord.garage_status_sample = {'sampled_at': self.coord._last_update_success_time,
                                          'state': self.status.state.name,
                                          'in_cleaning': self.status.in_cleaning,
                                          'in_returning': self.status.in_returning}


def test_first_handover_initializes_known_idle_only_after_two_real_polls():
    b=Boundaries()
    r=b.create()
    async def exercise():
        await r.async_restore()
        await r.async_tick()
        assert not r.session.known_idle
        b.advance()
        await r.async_tick()
        assert r.session.known_idle
        assert 'robot' in b.entry.options['garage_initialized_duids']
        assert 'robot' in b.store.data
        assert r.controller.phase == 'idle'
    asyncio.run(exercise())
    assert not any(name == 'door' for name,_ in b.events)


def test_reconciled_paused_session_exposes_original_resume_context_to_vacuum_entry():
    b = Boundaries(initialized=True)
    r = b.create()
    r._restored = True
    r._first_install = False
    r.session.begin(b.summary, b.utc, 'app_start')
    b.status.state.name = 'cleaning'
    b.status.in_cleaning = 1
    b.advance()
    r.controller.refresh()
    payload = r.session.to_dict()
    r.session.restore(payload)
    r.controller.fault('session_recovery_required')
    b.status.state.name = 'paused'
    b.status.in_cleaning = 0
    b.coord.last_resume_command = None
    b.advance()
    before = list(b.events)
    r.reset()
    assert b.coord.last_resume_command == 'app_resume'
    assert r.session.session_id == payload['session_id']
    assert not any(event[0] == 'door' for event in b.events[len(before):])


def test_lost_journal_after_initialization_is_not_first_install():
    b=Boundaries(initialized=True)
    r=b.create()
    asyncio.run(r.async_restore())
    assert r.controller.reason == 'session_recovery_required'


def test_fresh_observation_uses_successful_poll_id_not_periodic_tick():
    b=Boundaries()
    r=b.create()
    async def exercise():
        await r.async_restore()
        await r.async_tick()
        b.now += 1
        b.utc += 1
        await r.async_tick()
        assert not r.session.known_idle
    asyncio.run(exercise())


def test_cover_unavailable_retaining_100_does_not_grant_passage():
    b=Boundaries()
    r=b.create()
    b.cover.state='unavailable'
    r.controller.refresh()
    assert r.controller.passage_allowed is not True


def test_missing_real_charger_never_proves_safe_final_dock():
    b=Boundaries()
    r=b.create()
    b.coord.properties_api.map_content.map_data.charger=None
    assert r.robot_observation().position_trusted is False


def test_legacy_other_robot_cannot_open_an_owned_cover():
    m=runtime_module()
    b=Boundaries()
    r=b.create()
    b.hass.data[m.REGISTRY_KEY]={'cover.door':r}
    with pytest.raises(Exception,match='duplicate_binding'):
        m.check_legacy_ownership(b.hass, {'garage_guard_enabled':True,'garage_door_entity_id':'cover.door'}, 'other')


def test_unknown_raw_command_cannot_bypass_runtime():
    b=Boundaries()
    r=b.create()
    async def send():
        b.events.append(('robot',None))
    with pytest.raises(Exception,match='unsupported_guarded_command'):
        asyncio.run(r.async_send('opaque_move', send))
    assert ('robot',None) not in b.events


def test_issue_activation_and_resolution_are_deduplicated():
    b=Boundaries()
    r=b.create()
    r.notify('open_timeout')
    r.notify('open_timeout')
    r.notify(None)
    issues=[data for name,data in b.events if name=='roborock_plus_garage_issue']
    assert [d['active'] for d in issues] == [True,False]
    assert all(d['duid']=='robot' for d in issues)
