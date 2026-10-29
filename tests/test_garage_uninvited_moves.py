"""The two ways this integration could once have moved a door uninvited.

Both were found by auditing the call graph rather than by a failing test, so neither had
coverage. These pin them.

1. A clean command can be accepted and still never move the robot. The sequence used to
   find a docked, idle robot and call that "finished", closing the door for a task that
   never happened.

2. The explicit close service called ``close_door`` with no precondition, so any caller
   could shut the door on a robot standing in the doorway.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg.garage_door import GarageControlError
from _garage_pkg.garage_runtime import GarageRuntime


class Rig:
    """A robot that only moves when the test says so."""

    def __init__(self, *, opened=False, state='charging', docked=True, outside=False,
                 task_active=False, trusted=True):
        self.cover_calls = []
        self.opened = opened
        self.state = state
        self.docked = docked
        self.outside = outside
        self.task_active = task_active
        self.trusted = trusted
        self.clock_value = 0.0

    def door(self):
        return {
            'cover_available': True,
            'position': 100 if self.opened else 0,
            'open_contact': 'on' if self.opened else 'off',
            'close_contact': 'off' if self.opened else 'on',
        }

    def robot(self):
        return NS(state=self.state, docked=self.docked, outside_danger_zone=self.outside,
                  position_trusted=self.trusted, fresh=True, task_active=self.task_active)

    async def move(self, direction):
        self.cover_calls.append(direction)
        self.opened = direction == 'open'
        return {'ok': True}

    def clock(self) -> float:
        self.clock_value += 1.0
        return self.clock_value

    def build(self):
        binding = {
            'enabled': True, 'cover_entity_id': 'cover.door',
            'task_door_mode': 'after_exit',
            'open_source': 'contact', 'close_source': 'contact',
            'open_contact': 'binary_sensor.open',
            'close_contact': 'binary_sensor.closed',
            'open_contact_state': 'on', 'close_contact_state': 'on',
            'contact_settle_seconds': 0, 'handover_confirmed': True,
            'operation_timeout_seconds': 5,
        }
        runtime = GarageRuntime(
            NS(states=NS(get=lambda _: None), services=NS(), bus=NS(), data={}),
            NS(duid='robot', duid_slug='robot', config_entry=NS(options={}, entry_id='e'),
               last_update_success=True, update_interval=None, data=None,
               properties_api=NS(command=NS(send=_ok))),
            binding, store=None, zone_provider=lambda: None,
            clock=self.clock, utc=lambda: 1000.0,
        )
        runtime.controller.robot_observation = self.robot
        runtime.controller.door_observation = self.door
        runtime.controller.move_door = self.move
        runtime.controller.clock = self.clock
        runtime._sequence_sleep = _no_wait
        runtime.controller.refresh()
        return runtime


async def _ok(*args, **kwargs):
    return {'ok': True}


async def _no_wait(_seconds):
    await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


# --- 1. a clean that never started must not close the door -------------------

def test_a_clean_that_never_moved_the_robot_leaves_the_door_alone():
    """The command was accepted; the robot stayed on its dock; nothing should close.

    This is the sequence that used to close a door for a task that never happened.
    """
    rig = Rig(opened=True, state='charging', docked=True, outside=False, task_active=False)
    runtime = rig.build()
    runtime.controller.task_in_progress = True

    run(runtime._finish_sequence())

    assert rig.cover_calls == [], 'no door command for a clean that never ran'
    assert runtime.controller.task_in_progress is False, 'and the sequence gives up'


def test_the_sequence_does_not_close_before_the_robot_is_seen_working():
    """Waiting for proof is not the same as assuming success."""
    rig = Rig(opened=True, state='charging', docked=True, outside=False, task_active=False)
    runtime = rig.build()
    runtime.controller.task_in_progress = True

    seen = {'n': 0}
    original = runtime.controller.refresh

    def counting():
        original()
        seen['n'] += 1

    runtime.controller.refresh = counting
    run(runtime._finish_sequence())

    assert seen['n'] > 1, 'it polls for proof rather than deciding immediately'
    assert rig.cover_calls == []


def test_a_robot_docked_mid_task_does_not_count_as_started():
    """Docked with a live task is a recharge visit, not proof this clean is running.

    Asserting only "the door did not move" would also pass if the sequence accepted this
    as started and then waited forever. The difference is whether it gives up: a clean
    that never started is abandoned, and that is observable.
    """
    rig = Rig(opened=True, state='charging', docked=True, outside=False, task_active=True)
    runtime = rig.build()
    runtime.controller.task_in_progress = True

    run(runtime._finish_sequence())

    assert rig.cover_calls == [], 'a docked robot is not a robot at work'
    assert runtime.controller.task_in_progress is False, (
        'the sequence must abandon a clean it cannot see running, not wait on it')


def test_a_robot_away_with_no_task_does_not_count_as_started():
    """Away from the dock with nothing running is a robot coming home from elsewhere."""
    rig = Rig(opened=True, state='returning_home', docked=False, outside=True, task_active=False)
    runtime = rig.build()
    runtime.controller.task_in_progress = True

    run(runtime._finish_sequence())

    assert rig.cover_calls == [], 'not this clean, so not its door'
    assert runtime.controller.task_in_progress is False, (
        'and the sequence abandons it rather than treating the movement as its own')


# --- 2. the explicit close refuses a robot in the doorway --------------------

def test_an_explicit_close_is_refused_while_the_robot_is_in_the_doorway():
    rig = Rig(opened=True, state='cleaning', docked=False, outside=False, task_active=True)
    runtime = rig.build()

    with pytest.raises(GarageControlError) as err:
        run(runtime.async_request_close())

    assert err.value.reason == 'robot_in_the_way'
    assert rig.cover_calls == [], 'the door must not move'


def test_an_explicit_close_is_allowed_when_the_robot_is_outside():
    rig = Rig(opened=True, state='cleaning', docked=False, outside=True, task_active=True)
    runtime = rig.build()

    run(runtime.async_request_close())

    assert rig.cover_calls == ['close']


def test_an_explicit_close_is_allowed_when_the_robot_is_docked():
    rig = Rig(opened=True, state='charging', docked=True, outside=False, task_active=False)
    runtime = rig.build()

    run(runtime.async_request_close())

    assert rig.cover_calls == ['close']


def test_an_explicit_close_proceeds_when_the_position_is_unknown():
    """Unknown position is not a refusal.

    Refusing on missing data would turn a sensor fault into a door nobody can close,
    which is the class of fault this integration was rewritten to remove.
    """
    rig = Rig(opened=True, state='cleaning', docked=False, outside=False,
              task_active=True, trusted=False)
    runtime = rig.build()

    run(runtime.async_request_close())

    assert rig.cover_calls == ['close']
