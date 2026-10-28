"""The multi-stage service: one stage, one blocking call, one real outcome.

The old protocol returned a receipt immediately and the script polled the phase
sensor for ``running`` before accepting ``succeeded``. Both of those states needed a
task session alive between the two calls, so with sessions gone the wait could
never succeed -- it would sit until its own timeout, then stop the script.

The new contract is simpler and harder to get wrong: the service waits for the
stage to actually finish and returns what happened.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg.garage_door import GarageControlError
from _garage_pkg.garage_runtime import GarageRuntime


class Clock:
    """A clock that advances on every read.

    The wait loop is deadline-driven, so a frozen clock would spin forever. Tying
    the clock to the read count keeps the loop's real logic (refresh, check,
    compare against the deadline) while removing real time from the test.
    """

    def __init__(self):
        self.ticks = 0

    def __call__(self) -> float:
        self.ticks += 1
        return float(self.ticks)


class Rig:
    def __init__(self, *, opened=False):
        self.opened = opened
        self.cover_calls = []
        self.task_active = True
        self.docked = False
        self.state = 'cleaning'
        self.clock = Clock()
        self.refreshes = 0
        self.on_refresh = None

    def door(self):
        return {
            'cover_available': True,
            'position': 100 if self.opened else 0,
            'open_contact': 'on' if self.opened else 'off',
            'close_contact': 'off' if self.opened else 'on',
        }

    def robot(self):
        return NS(state=self.state, docked=self.docked, outside_danger_zone=True,
                  position_trusted=True, fresh=True, task_active=self.task_active)

    async def move(self, direction):
        self.cover_calls.append(direction)
        self.opened = direction == 'open'
        return {'ok': True}

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

        original_refresh = runtime.controller.refresh

        def counting_refresh():
            original_refresh()
            self.refreshes += 1
            if self.on_refresh is not None:
                self.on_refresh(self.refreshes)

        runtime.controller.refresh = counting_refresh
        counting_refresh()
        return runtime


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """The wait loop sleeps a second between checks; drop the wall clock."""
    real_sleep = asyncio.sleep

    async def immediate(_delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, 'sleep', immediate)


def run(coro):
    return asyncio.run(coro)


def test_a_stage_returns_succeeded_once_the_robot_is_done():
    rig = Rig(opened=True)
    runtime = rig.build()
    rig.task_active, rig.docked, rig.state = False, True, 'charging'

    result = run(runtime.async_start_stage(
        {'command': 'app_segment_clean', 'params': [{'segments': [3]}]},
        _ok,
    ))
    assert result['result'] == 'succeeded'
    assert result['reason'] is None


def test_a_stage_waits_while_the_robot_is_still_working():
    """The whole point of blocking: the caller must not move on early."""
    rig = Rig(opened=True)
    runtime = rig.build()

    def finish_after_three(seen):
        if seen == 3:
            rig.task_active, rig.docked, rig.state = False, True, 'charging'

    rig.on_refresh = finish_after_three

    result = run(runtime.async_start_stage(
        {'command': 'app_segment_clean', 'params': [{'segments': [3]}]},
        _ok,
    ))
    assert result['result'] == 'succeeded'
    assert rig.refreshes >= 3, 'it must not report success before the robot finishes'


def test_a_robot_parked_mid_task_does_not_end_the_stage():
    """Docked but still active is a recharge visit, not the end of the stage.

    This is the exact situation the household described: the robot goes back to charge
    in the middle of a job. The wait must not return, or the script starts the next room
    while the robot is still working.

    The wait function is exercised directly: ``async_start_stage`` also runs the clean
    sequence, whose own refreshes would mask what this test is about.
    """
    rig = Rig(opened=True)
    runtime = rig.build()
    rig.docked, rig.state = True, 'charging'      # parked, mid-task
    rig.task_active = True

    seen = {'n': 0}
    original = runtime.controller.refresh

    def counting():
        original()
        seen['n'] += 1
        if seen['n'] >= 4:
            rig.task_active = False

    runtime.controller.refresh = counting

    run(runtime._await_stage_completion(600))
    assert seen['n'] >= 4, 'the wait must continue while the task is live'


def test_a_robot_driving_home_does_not_end_the_stage():
    """A finished task away from the dock is not the end: the robot is still coming back."""
    rig = Rig(opened=True)
    runtime = rig.build()
    rig.task_active = False
    rig.docked, rig.state = False, 'returning_home'

    seen = {'n': 0}
    original = runtime.controller.refresh

    def counting():
        original()
        seen['n'] += 1
        if seen['n'] >= 3:
            rig.docked, rig.state = True, 'charging'

    runtime.controller.refresh = counting

    run(runtime._await_stage_completion(600))
    assert seen['n'] >= 3, 'the wait must continue until the robot is docked'


def test_a_stage_that_never_finishes_times_out_and_fails():
    """A timeout is not success: the script must hear about it."""
    rig = Rig(opened=True)
    runtime = rig.build()

    with pytest.raises(GarageControlError) as err:
        run(runtime.async_start_stage(
            {'command': 'app_segment_clean', 'params': [{'segments': [3]}],
             'timeout_minutes': 0.001},
            _ok,
        ))
    assert err.value.reason == 'stage_timeout'
    assert runtime.stage_result == 'failed'
    assert runtime.stage_reason == 'stage_timeout'


def test_the_door_opens_before_the_stage_command_is_sent():
    """A stage is a clean, so it gets the same proactive door handling.

    The sequence continues past the command, so the order also contains the close that
    follows when this robot is already home.
    """
    rig = Rig(opened=False)
    runtime = rig.build()
    rig.task_active, rig.docked, rig.state = False, True, 'charging'
    order = []

    async def fake_move(direction):
        order.append(direction)
        rig.opened = direction == 'open'

    runtime.controller.move_door = fake_move

    result = run(runtime.async_start_stage(
        {'command': 'app_segment_clean', 'params': [{'segments': [3]}]},
        _ok,
    ))
    assert order and order[0] == 'open', 'the door must open before the robot moves'
    assert result['result'] == 'succeeded'


def test_an_unsupported_stage_is_refused_before_anything_moves():
    rig = Rig(opened=True)
    runtime = rig.build()
    with pytest.raises(GarageControlError) as err:
        run(runtime.async_start_stage(
            {'command': 'app_charge', 'params': []},
            _ok,
        ))
    assert err.value.reason == 'invalid_stage'
    assert rig.cover_calls == []


def test_a_finished_task_away_from_the_dock_does_not_end_the_stage():
    """The robot still has to get home before the stage is over.

    A task can report finished while the robot is out in the room. Ending the stage
    there would let the script start the next room while the robot is still driving
    back -- before the door handling for its arrival has happened.
    """
    rig = Rig(opened=True)
    runtime = rig.build()
    rig.task_active = False                          # the job is over
    rig.docked, rig.state = False, 'returning_home'  # but it is not home yet

    seen = {'n': 0}
    original_refresh = runtime.controller.refresh

    def arrive_after_three():
        original_refresh()
        seen['n'] += 1
        if seen['n'] >= 3:
            rig.docked, rig.state = True, 'charging'

    runtime.controller.refresh = arrive_after_three

    result = run(runtime.async_start_stage(
        {'command': 'app_segment_clean', 'params': [{'segments': [3]}],
         'timeout_minutes': 10},
        _ok,
    ))
    assert result['result'] == 'succeeded'
    assert seen['n'] >= 3, 'it must wait for the robot to dock'


async def _ok(*args, **kwargs):
    return {'ok': True}
