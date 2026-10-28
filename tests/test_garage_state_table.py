"""The state table: which changes open the door, which close it.

The household summed it up as "the integration should grip firmly which state change
opens the door and which closes it". This file is that table, one test per row, plus the
manual-intervention cases that motivated it.

The door is the robot's only exit. So a closed door during a running task is a fault to
be corrected, not a preference to be respected -- that is the one case where the
integration overrides a household action.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg.garage_door import GarageControlError, GarageController


class Rig:
    def __init__(self, *, opened=False, task_active=False, state='charging',
                 docked=True, outside=False):
        self.calls = []
        self.opened = opened
        self.task_active = task_active
        self.state = state
        self.docked = docked
        self.outside = outside
        self.trusted = True
        self.fresh = True
        self.permit_close = True

    def door(self):
        return {
            'cover_available': True,
            'position': 100 if self.opened else 0,
            'open_contact': 'on' if self.opened else 'off',
            'close_contact': 'off' if self.opened else 'on',
        }

    def robot(self):
        return NS(state=self.state, docked=self.docked, outside_danger_zone=self.outside,
                  position_trusted=self.trusted, fresh=self.fresh,
                  task_active=self.task_active)

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
            close_allowed=lambda: self.permit_close,
            clock=lambda: 0.0,
        )


# --- row 1: a clean starts, the door opens before the robot is released ------

def test_opening_precedes_the_command():
    rig = Rig(opened=False)
    controller = rig.build()
    asyncio.run(controller.open_door())
    assert controller.open_confirmed() is True
    assert controller.task_in_progress is True


# --- row 2: the robot leaves the zone, the door closes behind it -------------

def test_leaving_the_zone_closes_the_door_behind_the_robot():
    rig = Rig(opened=True, task_active=True, state='cleaning', docked=False, outside=True)
    controller = rig.build()
    order = []

    async def pause():
        order.append('pause')
        rig.state = 'paused'

    async def resume():
        order.append('resume')

    assert asyncio.run(controller.update_after_departure(pause=pause, resume=resume)) is True
    assert order == ['pause', 'resume']
    assert rig.calls == ['close']


# --- row 3: the robot heads home, the door opens for it ----------------------

def test_a_return_opens_the_door():
    rig = Rig(opened=False, task_active=True, state='returning_home', docked=False, outside=True)
    controller = rig.build()
    order = []

    async def pause():
        order.append('pause')
        rig.state = 'paused'

    async def resume():
        order.append('resume')

    asyncio.run(controller.update_returning(pause=pause, resume=resume))
    assert order == ['pause', 'resume']
    assert rig.calls == ['open']


# --- row 4: mid-task at the dock with a closed door -> open it ---------------

@pytest.mark.parametrize("state", ['charging', 'charging_complete', 'idle'])
def test_a_docked_robot_with_a_live_task_gets_its_door_opened(state):
    """The household closed the door while the robot was recharging.

    The clean is still running and the robot will drive out again by itself. Leaving the
    door shut would put it in the robot's path, so this is the one case where the
    integration overrides a manual close.
    """
    rig = Rig(opened=False, task_active=True, state=state, docked=True)
    controller = rig.build()
    controller.task_in_progress = True

    assert asyncio.run(controller.sequence_finished()) is False
    assert rig.calls == ['open'], 'the robot needs a way out'
    assert controller.open_confirmed() is True


def test_the_reopened_door_is_not_closed_again_while_the_task_runs():
    """Once reopened the door must stay open, not oscillate."""
    rig = Rig(opened=False, task_active=True, state='charging', docked=True)
    controller = rig.build()
    controller.task_in_progress = True

    for _ in range(5):
        asyncio.run(controller.sequence_finished())
    assert rig.calls == ['open'], 'open once, then leave it alone'


def test_a_manual_open_mid_task_is_left_alone():
    """The household opened it: nothing to correct."""
    rig = Rig(opened=True, task_active=True, state='charging', docked=True)
    controller = rig.build()
    controller.task_in_progress = True
    asyncio.run(controller.sequence_finished())
    assert rig.calls == []


def test_a_manual_open_while_the_robot_works_is_left_alone():
    """Mid-task, robot out cleaning, door opened by hand: no reason to touch it."""
    rig = Rig(opened=True, task_active=True, state='cleaning', docked=False, outside=True)
    controller = rig.build()
    controller.task_in_progress = True
    asyncio.run(controller.sequence_finished())
    assert rig.calls == []


def test_a_manual_close_while_the_robot_works_is_left_alone():
    """The robot is out in the room; it will need the door when it returns.

    Closing it now is not this rule's business: the return handler opens the door when
    the robot actually heads home.
    """
    rig = Rig(opened=False, task_active=True, state='cleaning', docked=False, outside=True)
    controller = rig.build()
    controller.task_in_progress = True
    asyncio.run(controller.sequence_finished())
    assert rig.calls == [], 'no door command while the robot is working outside'


def test_a_docked_robot_whose_door_cannot_be_read_is_not_forced_open():
    """Blind is not a reason to move a door."""
    rig = Rig(opened=False, task_active=True, state='charging', docked=True)
    controller = rig.build()
    controller.task_in_progress = True

    def broken():
        return {'cover_available': False, 'position': None,
                'open_contact': None, 'close_contact': None}

    controller.door_observation = broken
    assert asyncio.run(controller.sequence_finished()) is False
    assert rig.calls == []
    assert controller.phase == 'blocked'


# --- row 5: the task is over and the robot is home -> close ------------------

def test_the_end_of_the_task_closes_the_door():
    rig = Rig(opened=True, task_active=False, state='charging', docked=True)
    controller = rig.build()
    controller.task_in_progress = True
    assert asyncio.run(controller.sequence_finished()) is True
    assert rig.calls == ['close']
    assert controller.phase == 'idle'


def test_a_task_that_ends_with_the_door_already_shut_commands_nothing():
    rig = Rig(opened=False, task_active=False, state='charging', docked=True)
    controller = rig.build()
    controller.task_in_progress = True
    assert asyncio.run(controller.sequence_finished()) is True
    assert rig.calls == []


def test_the_sequence_waits_while_the_robot_drives_home():
    rig = Rig(opened=False, task_active=False, state='returning_home', docked=False)
    controller = rig.build()
    controller.task_in_progress = True
    assert asyncio.run(controller.sequence_finished()) is False
    assert rig.calls == []


# --- outside a task, nothing at all -----------------------------------------

@pytest.mark.parametrize("opened", [True, False])
@pytest.mark.parametrize("docked", [True, False])
def test_outside_a_task_the_door_is_never_commanded(opened, docked):
    """The whole table above only applies inside a clean this integration started."""
    rig = Rig(opened=opened, task_active=False, state='charging' if docked else 'cleaning',
              docked=docked)
    controller = rig.build()
    assert controller.task_in_progress is False
    asyncio.run(controller.sequence_finished())
    assert rig.calls == []

# --- the two failure modes the mutations probe ------------------------------

def test_a_task_ending_with_an_unreadable_door_commands_nothing():
    """A blind controller must not guess at a close.

    The sequence still ends -- a stuck sensor must not leave a caller waiting forever --
    but the door is not touched, and the phase says why.
    """
    rig = Rig(opened=True, task_active=False, state='charging', docked=True)
    controller = rig.build()
    controller.task_in_progress = True

    def broken():
        return {'cover_available': False, 'position': None,
                'open_contact': None, 'close_contact': None}

    controller.door_observation = broken
    assert asyncio.run(controller.sequence_finished()) is True
    assert rig.calls == [], 'no command without readable evidence'
    assert controller.phase == 'blocked'


def test_a_manually_opened_door_mid_task_is_not_closed():
    """Docked mid-task with the door already open: the rule is satisfied.

    Closing it here would fight the household every time they opened the door during a
    clean, which is the behaviour this table exists to prevent.
    """
    rig = Rig(opened=True, task_active=True, state='charging', docked=True)
    controller = rig.build()
    controller.task_in_progress = True

    for _ in range(3):
        assert asyncio.run(controller.sequence_finished()) is False
    assert rig.calls == []
