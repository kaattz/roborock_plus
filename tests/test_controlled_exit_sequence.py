"""Controlled exit: drive out, close the door, then clean.

Live geometry this encodes (2026-10-25): the robot parks at (25696, 28439) inside
a danger zone whose lower edge is y=27123. Left alone it swings right on departure
and grazes the closed door beside the cabinet -- only 373mm of clearance. The fix
gives it one waypoint straight out and does not interrupt it on the way.

An earlier design paused the robot on ``docked -> cleaning`` to open the door.
That failed in the field: the state change arrives after the robot has already
moved, so the pause landed mid-manoeuvre in a tight opening and the resumed robot
spun in place forever. Nothing here interrupts the robot.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from test_garage_runtime import Boundaries, binding


TARGET = (25696, 26500)


class ExitRig:
    """A controller wired to a scriptable robot, with no Home Assistant."""

    def __init__(self):
        self.b = Boundaries(initialized=True)
        self.b.entry.options['garage_bindings']['robot'] = dict(
            binding(),
            controlled_exit_enabled=True,
            controlled_exit_x=TARGET[0],
            controlled_exit_y=TARGET[1],
            controlled_exit_timeout_seconds=120,
        )
        self.runtime = self.b.create()
        self.sent = []
        self.position = NS(x=25696.0, y=28439.0)
        self.outside = False
        self.trusted = True
        self.fresh = True
        self.docked = True

    def install(self):
        controller = self.runtime.controller
        controller.phase = 'idle'
        controller.reason = None

        async def send_robot(command, params=None):
            self.sent.append((command, params))
            if command == 'app_goto_target':
                # The robot drives: move it to the waypoint on the next poll.
                self.position = NS(x=float(params[0]), y=float(params[1]))
                self.outside = True
                self.docked = False
            return {'ok': True}

        controller.send_robot = send_robot

        def robot_observation():
            base = self.b.create_observation() if hasattr(self.b, 'create_observation') else None
            return NS(
                now=self.b.now, utc=self.b.utc, state='idle', in_cleaning=0,
                in_returning=0, summary=self.b.summary, position_trusted=self.trusted,
                docked=self.docked, fresh=self.fresh, sample_id=1,
                outside_danger_zone=self.outside, position_sample_id=1,
                position=self.position,
            )

        controller.robot_observation = robot_observation
        controller.robot = robot_observation()
        return controller


# --- the happy path ----------------------------------------------------------

def test_the_robot_is_driven_to_the_waypoint_then_the_door_is_closed():
    rig = ExitRig()
    controller = rig.install()
    door_calls = []

    async def move_door(direction, operation):
        door_calls.append(direction)

    async def no_wait(direction, operation):
        return None

    async def persist():
        return None
    controller.move_door = move_door
    controller._wait = no_wait
    controller.persist = persist

    asyncio.run(controller.async_run_controlled_exit(
        {'x': TARGET[0], 'y': TARGET[1], 'timeout': 120.0}))

    assert ('app_goto_target', [TARGET[0], TARGET[1]]) in rig.sent, (
        'the waypoint must be sent as coordinates')
    assert door_calls[-1] == 'close', 'the door must be shut at the waypoint'


def test_goto_carries_the_configured_coordinates():
    rig = ExitRig()
    controller = rig.install()

    async def move_door(direction, operation):
        return None

    async def no_wait(direction, operation):
        return None

    async def persist():
        return None

    controller.move_door = move_door
    controller._wait = no_wait
    controller.persist = persist
    asyncio.run(controller.async_run_controlled_exit(
        {'x': 25500, 'y': 26000, 'timeout': 60.0}))
    got = [c for c in rig.sent if c[0] == 'app_goto_target']
    assert got == [('app_goto_target', [25500, 26000])]


# --- arrival is judged by position, not by a guess ---------------------------

def test_a_position_outside_the_tolerance_never_counts_as_arrival():
    rig = ExitRig()
    controller = rig.install()
    controller._controlled_exit_target = TARGET
    rig.position = NS(x=25696.0, y=27800.0)      # 700mm short
    assert controller._controlled_exit_arrived() is False


def test_a_position_inside_the_tolerance_counts_as_arrival():
    rig = ExitRig()
    controller = rig.install()
    controller._controlled_exit_target = TARGET
    rig.position = NS(x=25800.0, y=26550.0)      # ~120mm off
    controller.refresh()
    assert controller._controlled_exit_arrived() is True


def test_an_untrusted_position_never_counts_as_arrival():
    rig = ExitRig()
    controller = rig.install()
    controller._controlled_exit_target = TARGET
    rig.position = NS(x=TARGET[0], y=TARGET[1])
    rig.trusted = False
    controller.refresh()
    assert controller._controlled_exit_arrived() is False


def test_a_missing_position_never_counts_as_arrival():
    rig = ExitRig()
    controller = rig.install()
    controller._controlled_exit_target = TARGET
    rig.position = None
    controller.refresh()
    assert controller._controlled_exit_arrived() is False


# --- the wait never outruns its deadline ------------------------------------

def test_the_wait_gives_up_at_the_deadline():
    rig = ExitRig()
    controller = rig.install()
    rig.position = NS(x=25696.0, y=28439.0)      # never moves
    controller._controlled_exit_target = TARGET
    controller._controlled_exit_deadline = controller.clock() + 0.0

    with pytest.raises(Exception) as err:
        asyncio.run(controller._wait_controlled_exit(controller._acquire()))
    assert 'controlled_exit_failed' in str(getattr(err.value, 'reason', err.value))


# --- the door stays open for the whole trip ---------------------------------

def test_exit_never_closes_the_door_while_the_robot_is_driving():
    """``after_exit`` must stand down for the whole controlled exit."""
    rig = ExitRig()
    controller = rig.install()
    controller.binding = dict(controller.binding, task_door_mode='after_exit')
    controller.phase = 'controlled_exit'
    controller._exit_due = True
    controller._departure_armed = False
    assert asyncio.run(controller._update_after_exit()) is True


def test_after_exit_resumes_once_the_exit_phase_is_over():
    rig = ExitRig()
    controller = rig.install()
    controller.binding = dict(controller.binding, task_door_mode='after_exit')
    controller.phase = 'outside_closed'
    assert asyncio.run(controller._update_after_exit()) is not True


# --- closing is refused unless the facts support it -------------------------

def test_closing_is_refused_when_the_robot_is_not_confirmed_outside():
    rig = ExitRig()
    controller = rig.install()
    rig.outside = False
    with pytest.raises(Exception) as err:
        asyncio.run(controller._close_after_controlled_exit(controller._acquire()))
    assert 'controlled_exit_failed' in str(getattr(err.value, 'reason', err.value))


def test_closing_is_refused_when_the_family_permission_is_off():
    rig = ExitRig()
    controller = rig.install()
    rig.outside = True
    controller.close_allowed = lambda: False
    with pytest.raises(Exception) as err:
        asyncio.run(controller._close_after_controlled_exit(controller._acquire()))
    assert 'close_not_allowed' in str(getattr(err.value, 'reason', err.value))
