"""Exercise the real controller with a clock and boundary adapters, never devices."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


PATH = Path(__file__).resolve().parents[1] / 'custom_components/roborock_plus/garage_control.py'


def module():
    assert PATH.exists(), 'The standard garage controller is not implemented'
    if 'garage_control_under_test' in sys.modules:
        return sys.modules['garage_control_under_test']
    spec = importlib.util.spec_from_file_location('garage_control_under_test', PATH)
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


class Boundary:
    def __init__(self):
        self.now = 0.0
        self.events = []
        self.open = False
        self.closed = False
        self.available = True
        self.permission = True
        self.after_sleep = None
        self.session = SimpleNamespace(active=False, started=False, ended=False,
                                       known_idle=True, can_close=True, can_begin=True, reason=None, session_id=None)
        self.session.observe = lambda observation: None
        self.session.begin = self.begin
        self.session.initialize_idle = lambda: setattr(self.session, 'known_idle', True)
        self.session.to_dict = lambda: {}
        self.evidence = SimpleNamespace(observe=self.observe, note_command=self.note_command,
                                        invalidate=lambda reason: None)

    def begin(self, summary, utc, command='app_start'):
        self.session.active = True
        self.session.can_close = False
        self.session.session_id = 'session'

    def observe(self, now, **kwargs):
        op = SimpleNamespace(reached=self.open, quality='confirmed', reason=None, source='contact')
        cl = SimpleNamespace(reached=self.closed, quality='confirmed', reason=None, source='contact')
        return SimpleNamespace(open=op, close=cl, passage_allowed=self.open if self.available else None,
                               open_confirmed=self.open, closed_confirmed=self.closed)

    def note_command(self, direction, now, operation_id):
        self.events.append(('accepted', direction))

    async def move(self, direction, operation_id):
        self.events.append(('door', direction))

    async def sleep(self, seconds):
        self.now += seconds
        if self.after_sleep:
            self.after_sleep()
        await asyncio.sleep(0)

    async def persist(self):
        self.events.append(('persist', self.session.active))

    def create(self):
        m = module()
        self.controller = m.GarageController(
            {'operation_timeout_seconds': 2, 'allow_estimated': False},
            evidence=self.evidence, session=self.session, move_door=self.move,
            door_observation=lambda: {'cover_available': self.available},
            robot_observation=lambda: SimpleNamespace(utc=1000+self.now, state='cleaning',
                                                       docked=False, fresh=True, summary={}),
            close_allowed=lambda: self.permission,
            persist=self.persist, notify=lambda reason: self.events.append(('issue', reason)),
            clock=lambda: self.now, sleep=self.sleep,
        )
        return self.controller


def test_opens_and_rechecks_before_original_command_and_persists_first():
    b = Boundary()
    c = b.create()
    b.after_sleep = lambda: setattr(b, 'open', b.now >= 1)

    async def send():
        assert b.open
        b.events.append(('robot', 'app_start'))

    asyncio.run(c.async_execute('app_start', send, new_task=True))
    assert b.now >= 1
    assert b.events.index(('door', 'open')) < b.events.index(('robot', 'app_start'))
    assert b.events.index(('persist', True)) < b.events.index(('robot', 'app_start'))


def test_timeout_never_sends_robot_or_compensating_close():
    b = Boundary()
    c = b.create()
    async def send():
        b.events.append(('robot', 'start'))
    with pytest.raises(module().GarageControlError, match='open_timeout'):
        asyncio.run(c.async_execute('app_start', send, new_task=True))
    assert not any(e[0] == 'robot' or e == ('door', 'close') for e in b.events)
    assert c.phase == 'blocked'


def test_manual_override_cancels_old_wait_and_cannot_be_cleared_by_feedback():
    b = Boundary()
    c = b.create()
    b.after_sleep = lambda: c.external_command('close')
    async def send():
        b.events.append(('robot', 'start'))
    with pytest.raises(Exception, match='manual_override'):
        asyncio.run(c.async_execute('app_start', send, new_task=True))
    b.open = True
    asyncio.run(c.async_update())
    assert c.phase == 'manual_hold'
    assert not any(e[0] == 'robot' for e in b.events)


def test_failed_durable_session_prevents_robot_command():
    b = Boundary()
    c = b.create()
    b.open = True
    async def broken():
        raise OSError('storage failed')
    c.persist = broken
    async def send():
        b.events.append(('robot', 'start'))
    with pytest.raises(Exception, match='storage failed'):
        asyncio.run(c.async_execute('app_start', send, new_task=True))
    assert not any(e[0] == 'robot' for e in b.events)
    assert c.phase == 'blocked'


def test_permission_is_rechecked_after_async_session_save_before_send():
    b = Boundary()
    c = b.create()
    b.open = True
    async def changed_during_save():
        b.open = False
    c.persist = changed_during_save
    async def send():
        b.events.append(('robot', 'start'))
    with pytest.raises(Exception, match='feedback_unavailable'):
        asyncio.run(c.async_execute('app_start', send, new_task=True))
    assert not any(e[0] == 'robot' for e in b.events)


def test_active_session_rejects_second_clean_task():
    b = Boundary()
    c = b.create()
    b.open = True
    b.session.active = True
    async def send():
        b.events.append(('robot', 'start'))
    with pytest.raises(Exception, match='task_already_active'):
        asyncio.run(c.async_execute('app_start', send, new_task=True))
    assert not b.events


def test_intermission_does_not_close_even_when_robot_is_charging():
    b = Boundary()
    c = b.create()
    b.open = True
    b.session.active = True
    b.session.started = True
    b.session.can_close = False
    c.phase = 'task_open'
    asyncio.run(c.async_update())
    assert ('door', 'close') not in b.events


def test_idle_household_close_uses_same_safety_and_permission_checks():
    b = Boundary()
    c = b.create()
    b.permission = False
    with pytest.raises(Exception, match='close_not_allowed'):
        asyncio.run(c.async_request_close())
    assert ('door', 'close') not in b.events
    b.permission = True
    b.session.can_close = False
    with pytest.raises(Exception, match='task_end_unconfirmed'):
        asyncio.run(c.async_request_close())
    assert ('door', 'close') not in b.events


def test_permission_revocation_interrupts_close_and_stops_if_supported():
    b = Boundary()
    c = b.create()
    c.supports_stop = lambda: True
    b.after_sleep = lambda: setattr(b, 'permission', False)
    with pytest.raises(Exception, match='close_not_allowed'):
        asyncio.run(c.async_request_close())
    assert ('door', 'stop') in b.events
    assert c.phase == 'blocked'


def test_successful_close_reports_idle_but_no_fake_confirmed_position():
    b = Boundary()
    c = b.create()
    b.after_sleep = lambda: setattr(b, 'closed', b.now >= 1)
    asyncio.run(c.async_request_close())
    assert b.events.count(('door', 'close')) == 1
    assert c.phase == 'idle'


def test_shutdown_invalidates_sleeping_operation():
    b = Boundary()
    c = b.create()
    b.after_sleep = c.shutdown
    async def send():
        b.events.append(('robot', 'start'))
    with pytest.raises(Exception, match='control_unloaded'):
        asyncio.run(c.async_execute('app_start', send, new_task=True))
    assert not any(e[0] == 'robot' for e in b.events)


def test_superseded_generation_cannot_finish_an_old_close_wait():
    b = Boundary()
    c = b.create()
    def supersede():
        c.operation_id += 1
        b.closed = True
    b.after_sleep = supersede
    with pytest.raises(Exception, match='operation_cancelled'):
        asyncio.run(c.async_request_close())
    assert c.phase == 'blocked'
    assert not any(e[0] == 'persist' for e in b.events)


def test_explicit_resume_is_allowed_and_auto_resume_is_never_invented():
    b = Boundary()
    c = b.create()
    b.open = True
    b.session.active = True
    b.session.started = True
    async def send():
        b.events.append(('robot', 'app_resume'))
    asyncio.run(c.async_execute('app_resume', send, resume=True))
    assert ('robot', 'app_resume') in b.events
    asyncio.run(c.async_update())
    assert b.events.count(('robot', 'app_resume')) == 1
