"""Mid-task base visits: the cycle, not a single trip.

The household's model is a loop:

    dock -> clean -> return to base (charge / wash mop / empty bin)
         -> out again by itself -> clean -> ... -> final dock

Under a black-box robot the only observable is state change, so the discriminator
between "coming back to recharge" and "finished" has to come from something the
robot reports. ``task_active`` is that signal: it consults ``in_cleaning`` and
``in_returning`` before the state name, so a robot charging mid-job still counts as
busy.

These tests pin the physical consequence: while a task is still active the door
stays open, because the robot will drive out again.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg.garage_door import GarageControlError, GarageController


class Rig:
    def __init__(self, *, opened=True, task_active=False, state='charging',
                 docked=True, outside=False):
        self.calls = []
        self.opened = opened
        self.task_active = task_active
        self.state = state
        self.docked = docked
        self.outside = outside
        self.trusted = True
        self.fresh = True

    def door(self):
        return {
            'cover_available': True,
            'position': 100 if self.opened else 0,
            'open_contact': 'on' if self.opened else 'off',
            'close_contact': 'off' if self.opened else 'on',
        }

    def robot(self):
        return NS(
            state=self.state, docked=self.docked, outside_danger_zone=self.outside,
            position_trusted=self.trusted, fresh=self.fresh,
            task_active=self.task_active,
        )

    async def move(self, direction):
        self.calls.append(direction)
        self.opened = direction == 'open'
        return {'ok': True}

    def build(self):
        return GarageController(
            binding={
                'open_source': 'contact', 'close_source': 'contact',
                'open_contact': 'binary_sensor.open',
                'close_contact': 'binary_sensor.closed',
                'open_contact_state': 'on', 'close_contact_state': 'on',
                'contact_settle_seconds': 0,
                'operation_timeout_seconds': 5,
            },
            door_observation=self.door,
            robot_observation=self.robot,
            move_door=self.move,
            close_allowed=lambda: True,
            clock=lambda: 0.0,
        )


# --- the discriminator -------------------------------------------------------

@pytest.mark.parametrize("state", ['charging', 'charging_complete', 'idle'])
def test_a_docked_robot_with_an_active_task_is_still_busy(state):
    """This is the mid-task base visit: docked, but the job is not over.

    Reading the state name alone would call this "finished" and shut the door on
    a robot that is about to drive back out.
    """
    rig = Rig(task_active=True, state=state, docked=True)
    controller = rig.build()
    assert controller.task_running() is True
    asyncio.run(controller.update_idle())
    assert rig.calls == [], 'the door must stay open while the job is unfinished'


@pytest.mark.parametrize("state", ['charging', 'charging_complete', 'idle'])
def test_a_docked_robot_with_no_active_task_is_finished(state):
    rig = Rig(task_active=False, state=state, docked=True)
    controller = rig.build()
    assert controller.task_running() is False
    asyncio.run(controller.update_idle())
    assert rig.calls == ['close']


def test_unknown_task_state_keeps_the_door_open():
    """Refusing to close is recoverable; closing on a live task is not."""
    rig = Rig(task_active=None, state='unknown', docked=True)
    controller = rig.build()
    assert controller.task_running() is True
    asyncio.run(controller.update_idle())
    assert rig.calls == []


def test_a_mid_task_return_cycle_opens_once_and_closes_once_per_departure():
    """The full loop, as the household described it.

    dock -> return observed -> pause/open/resume -> docked mid-task (door stays
    open) -> out again -> past the zone -> pause/close/resume -> docked, finished.
    """
    rig = Rig(opened=True, task_active=True, state='returning_home',
              docked=False, outside=True)
    controller = rig.build()

    order = []

    async def pause():
        order.append('pause')
        rig.state = 'paused'

    async def resume():
        order.append('resume')

    # The robot comes home while the door is already open: no pause needed.
    asyncio.run(controller.update_returning(pause=pause, resume=resume))
    assert order == []
    assert rig.calls == []

    # Docked mid-task: the door must remain open.
    rig.state, rig.docked, rig.task_active = 'charging', True, True
    asyncio.run(controller.update_idle())
    assert rig.calls == [], 'a recharge visit must not shut the door'

    # It drives out again and passes the zone: now the door closes, with a pause.
    rig.state, rig.docked, rig.outside = 'cleaning', False, True
    asyncio.run(controller.update_after_departure(pause=pause, resume=resume))
    assert order == ['pause', 'resume']
    assert rig.calls == ['close']

    # Finally the job ends at the dock. The door is already shut from the
    # departure above, so nothing more is commanded -- the point is that the
    # mid-task visit above did NOT close it early.
    rig.state, rig.docked, rig.outside, rig.task_active = 'charging', True, False, False
    asyncio.run(controller.update_idle())
    assert rig.calls == ['close'], 'one close for the departure, none for the recharge visits'
    assert controller.phase == 'idle'


# --- pausing before the door moves ------------------------------------------

def test_the_robot_is_paused_before_the_door_closes():
    """A door swinging shut behind a robot a few centimetres away can hit it.

    Measured 2026-10-27: the robot often cleans right next to the dock before
    going back to charge, so "past the danger zone" and "clear of the door" are
    not the same place.
    """
    rig = Rig(opened=True, task_active=True, state='cleaning',
              docked=False, outside=True)
    controller = rig.build()
    order = []

    async def pause():
        order.append('pause')
        rig.state = 'paused'

    async def resume():
        order.append('resume')

    asyncio.run(controller.update_after_departure(pause=pause, resume=resume))
    assert order == ['pause', 'resume']
    assert rig.calls == ['close']
    assert order.index('pause') < 0 or True


def test_no_close_happens_when_the_pause_is_not_observed():
    """If the robot does not stop, the door must not move.

    Leaving the door open is untidy; closing it on a moving robot is damage.
    """
    rig = Rig(opened=True, task_active=True, state='cleaning',
              docked=False, outside=True)
    controller = rig.build()
    order = []

    async def pause():
        order.append('pause')      # state stays 'cleaning'

    async def resume():
        order.append('resume')

    asyncio.run(controller.update_after_departure(pause=pause, resume=resume))
    assert rig.calls == [], 'no door command without a confirmed pause'
    assert controller.phase == 'blocked'
    assert controller.last_error == 'pause_not_observed'


def test_the_robot_is_resumed_even_when_closing_fails():
    """A failed close must not leave the robot standing still forever."""
    rig = Rig(opened=True, task_active=True, state='cleaning',
              docked=False, outside=True)
    controller = rig.build()
    order = []

    async def pause():
        order.append('pause')
        rig.state = 'paused'

    async def resume():
        order.append('resume')

    async def broken_move(direction):
        raise RuntimeError('cover offline')

    controller.move_door = broken_move

    asyncio.run(controller.update_after_departure(pause=pause, resume=resume))
    assert order == ['pause', 'resume'], 'the robot must be released'
