"""End-to-end behaviour of the garage runtime, against the real controller.

This replaces the suites that pinned the retired task-attribution machinery. Those
tested *how* the integration decided (sessions, correlation flags, reset
semantics); these test *what the household experiences*:

    a clean opens the door first,
    the door closes once the robot is out,
    a return pauses the robot, opens, resumes,
    and nothing ever latches waiting for a human.

The distinction matters: a test that pins a decision procedure has to be rewritten
whenever the procedure changes, while a test that pins an outcome keeps its value.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg.garage_door import GarageControlError
from _garage_pkg.garage_runtime import GarageRuntime


class FakeStates:
    def __init__(self):
        self._states = {}

    def set(self, entity_id, state, **attributes):
        self._states[entity_id] = NS(state=state, attributes=dict(attributes), context=None)

    def get(self, entity_id):
        return self._states.get(entity_id)


class FakeServices:
    def __init__(self, on_call):
        self.calls = []
        self._on_call = on_call

    async def async_call(self, domain, service, data, blocking=False, context=None):
        self.calls.append((domain, service, data.get('entity_id')))
        self._on_call(service, context)
        return None


class FakeBus:
    def __init__(self):
        self.fired = []
        self._listeners = []

    def async_fire(self, event, data):
        self.fired.append((event, data))

    def async_listen(self, event, callback):
        self._listeners.append((event, callback))
        return lambda: None


class FakeCoordinator:
    def __init__(self):
        self.duid = 'robot'
        self.duid_slug = 'robot'
        self.last_update_success = True
        self.update_interval = __import__('datetime').timedelta(seconds=1)
        self.garage_status_sample = None
        self.last_map_position_update = None
        self.garage_vacuum_entity_id = 'vacuum.robot'
        self.last_resume_command = None
        self.config_entry = NS(options={}, entry_id='entry')
        self.data = NS(clean_summary=None)
        self._state = 'charging'
        self._docked = True
        self._outside = False
        self._trusted = True
        self._position = NS(x=0.0, y=0.0)
        self.properties_api = NS(
            map_content=NS(map_data=NS(charger=NS(x=0.0, y=0.0))),
            command=NS(send=self._send),
        )
        self.robot_commands = []

    async def _send(self, name, params=None):
        self.robot_commands.append((name, params))
        # A real robot reports paused when it accepts app_pause; without this the
        # controller would correctly refuse to move the door.
        if name == 'app_pause':
            self._state = 'paused'
            if self.garage_status_sample is not None:
                self.garage_status_sample['state'] = 'paused'
        return {'ok': True}

    async def async_refresh(self):
        return None

    def resolve_vacuum_position(self):
        return NS(x=self._position.x, y=self._position.y, trusted=self._trusted)

    def sample(self, state, *, docked, outside, trusted=True, now=1000.0):
        import datetime as dt
        self._state = state
        self._docked = docked
        self._outside = outside
        self._trusted = trusted
        # The zone is +/-10000; the robot must actually be outside it for
        # outside_danger_zone to be computed as true.
        self._position = NS(x=0.0, y=50000.0 if outside else 0.0)
        self.garage_status_sample = {
            'sampled_at': dt.datetime.fromtimestamp(now, dt.timezone.utc),
            'state': state, 'in_cleaning': 1 if state == 'cleaning' else 0,
            'in_returning': 1 if state == 'returning_home' else 0,
        }
        self.last_map_position_update = dt.datetime.fromtimestamp(now, dt.timezone.utc)


def build_runtime(*, opened=False, permission=True):
    states = FakeStates()
    coordinator = FakeCoordinator()
    zone = NS(min_x=-10000, max_x=10000, min_y=-10000, max_y=10000)

    def door_entity(open_now: bool):
        states.set('cover.door', 'open' if open_now else 'closed',
                   current_position=100 if open_now else 0)
        states.set('binary_sensor.open', 'on' if open_now else 'off')
        states.set('binary_sensor.closed', 'off' if open_now else 'on')

    door_entity(opened)

    def on_cover(service, context):
        door_entity(service == 'open_cover')

    services = FakeServices(on_cover)
    binding = {
        'enabled': True,
        'cover_entity_id': 'cover.door',
        'task_door_mode': 'after_exit',
        'open_source': 'contact',
        'close_source': 'contact',
        'open_contact': 'binary_sensor.open',
        'close_contact': 'binary_sensor.closed',
        'open_contact_state': 'on',
        'close_contact_state': 'on',
        'contact_settle_seconds': 0,
        'handover_confirmed': True,
        'operation_timeout_seconds': 5,
        'close_permission_entity': 'input_boolean.permit' if not permission else None,
    }
    hass = NS(states=states, services=services, bus=FakeBus(), data={})
    runtime = GarageRuntime(
        hass, coordinator, binding, store=None,
        zone_provider=lambda: zone,
        clock=lambda: 0.0, utc=lambda: 1000.0,
    )
    states.set('input_boolean.permit', 'off' if not permission else 'on')
    return runtime, coordinator, services, door_entity


# --- a clean opens the door first -------------------------------------------

def test_a_clean_opens_and_confirms_the_door_before_anything_is_sent():
    """Ordering is the whole safety property: door confirmed, then the command."""
    runtime, coordinator, services, _ = build_runtime(opened=False)
    coordinator.sample('charging', docked=True, outside=False)
    seen = []

    async def send():
        seen.append(('robot_command', runtime.controller.open_confirmed()))
        return {'ok': True}

    asyncio.run(runtime.async_send('app_segment_clean', send))
    assert services.calls == [('cover', 'open_cover', 'cover.door')]
    assert seen == [('robot_command', True)], 'the robot must not be released before the door is confirmed'


def test_an_already_open_door_is_not_reopened():
    runtime, coordinator, services, _ = build_runtime(opened=True)
    coordinator.sample('charging', docked=True, outside=False)

    async def send():
        return {'ok': True}

    asyncio.run(runtime.async_send('app_segment_clean', send))
    assert services.calls == []


def test_unusable_door_evidence_refuses_the_clean_without_latching():
    """No usable evidence means no release -- and no state that needs clearing."""
    runtime, coordinator, services, _ = build_runtime(opened=False)
    runtime.hass.states.set('cover.door', 'unavailable')
    coordinator.sample('charging', docked=True, outside=False)

    async def send():
        raise AssertionError('the robot must not be released with unknown door state')

    with pytest.raises(GarageControlError) as err:
        asyncio.run(runtime.async_send('app_segment_clean', send))
    assert err.value.reason == 'feedback_unavailable'
    assert services.calls == []
    assert runtime.controller.phase == 'blocked'

    # Recovery needs no operator: the next good sample works.
    build = build_runtime(opened=False)
    runtime2, coordinator2, services2, _ = build
    coordinator2.sample('charging', docked=True, outside=False)
    asyncio.run(runtime2.async_request_open())
    assert services2.calls == [('cover', 'open_cover', 'cover.door')]


# --- the door closes once the robot is out ----------------------------------

def test_the_door_stays_open_while_the_robot_is_still_inside():
    runtime, coordinator, services, _ = build_runtime(opened=True)
    coordinator.sample('cleaning', docked=False, outside=False)
    asyncio.run(runtime.async_tick())
    assert services.calls == []


def test_the_door_closes_once_the_robot_is_confirmed_outside():
    runtime, coordinator, services, _ = build_runtime(opened=True)
    coordinator.sample('cleaning', docked=False, outside=True)
    asyncio.run(runtime.async_tick())
    assert services.calls == [('cover', 'close_cover', 'cover.door')]


def test_an_untrusted_position_does_not_close_the_door():
    runtime, coordinator, services, _ = build_runtime(opened=True)
    coordinator.sample('cleaning', docked=False, outside=True, trusted=False)
    asyncio.run(runtime.async_tick())
    assert services.calls == []


# --- returning ---------------------------------------------------------------

def test_a_return_pauses_then_opens_then_resumes():
    runtime, coordinator, services, _ = build_runtime(opened=False)
    coordinator.sample('returning_home', docked=False, outside=True)
    order = []

    async def fake_control(name, params=None):
        order.append(name)
        if name == 'app_pause':
            coordinator.sample('paused', docked=False, outside=True)
        return None

    runtime.async_control_command = fake_control

    asyncio.run(runtime._handle_return())
    assert order == ['app_pause', 'app_resume'], 'pause first, resume last'
    assert services.calls == [('cover', 'open_cover', 'cover.door')], (
        'the door opens between the pause and the resume')


async def _noop():
    return None


def test_a_return_with_an_open_door_needs_no_pause():
    runtime, coordinator, services, _ = build_runtime(opened=True)
    coordinator.sample('returning_home', docked=False, outside=True)
    called = []

    async def fake(name, params=None):
        called.append(name)

    runtime.async_control_command = fake
    asyncio.run(runtime._handle_return())
    assert called == []


# --- final close -------------------------------------------------------------

def test_the_door_closes_when_the_robot_is_docked_and_idle_again():
    runtime, coordinator, services, _ = build_runtime(opened=True)
    coordinator.sample('charging', docked=True, outside=False)
    asyncio.run(runtime.async_tick())
    assert services.calls == [('cover', 'close_cover', 'cover.door')]


def test_a_running_task_is_left_alone():
    runtime, coordinator, services, _ = build_runtime(opened=True)
    coordinator.sample('cleaning', docked=False, outside=False)
    asyncio.run(runtime.async_tick())
    assert services.calls == []


# --- the family permission ---------------------------------------------------

def test_the_permission_blocks_closing():
    """A household that asked to close manually must be obeyed.

    The refusal is propagated rather than swallowed, so the caller learns the
    door stayed open instead of assuming it shut.
    """
    runtime, coordinator, services, _ = build_runtime(opened=True, permission=False)
    coordinator.sample('charging', docked=True, outside=False)
    with pytest.raises(GarageControlError) as err:
        asyncio.run(runtime.async_tick())
    assert err.value.reason == 'close_not_allowed'
    assert services.calls == [], 'the door must not be commanded'


# --- no latches anywhere -----------------------------------------------------

def test_repeated_failures_leave_no_state_that_needs_clearing():
    """The property the whole rewrite exists for.

    Five unusable-evidence attempts in a row, then a good sample: the door opens.
    Nothing had to be reset, released, or acknowledged.
    """
    runtime, coordinator, services, door_entity = build_runtime(opened=False)
    coordinator.sample('charging', docked=True, outside=False)
    for _ in range(5):
        runtime.hass.states.set('cover.door', 'unavailable')
        with pytest.raises(GarageControlError):
            asyncio.run(runtime.async_send('app_segment_clean', lambda: _noop()))
    door_entity(False)
    asyncio.run(runtime.async_request_open())
    assert services.calls == [('cover', 'open_cover', 'cover.door')]
    assert runtime.controller.last_error is None


def test_reset_is_only_an_observation_refresh():
    """reset() must not be the difference between usable and unusable."""
    runtime, coordinator, services, _ = build_runtime(opened=False)
    coordinator.sample('charging', docked=True, outside=False)
    runtime.reset()
    assert services.calls == [], 'reset must never command hardware'
    asyncio.run(runtime.async_request_open())
    assert services.calls == [('cover', 'open_cover', 'cover.door')]
