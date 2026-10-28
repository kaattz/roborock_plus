"""The door is shared, so the integration only moves it inside a clean task.

Reported 2026-10-28: pressing the wall button opened the door and it shut again a few
seconds later; opening it by hand to full also closed it.

The cause was ``update_idle``, which closed whenever the robot was docked and idle and
the door stood open. That is true every second of an ordinary evening, so the condition
said nothing about a task. It then reset its phase to ``idle`` after closing, which is
why no transition appeared in the history and the integration looked innocent.

The rule that replaces it is scope, not ownership: the integration's permission to move
this door comes from the clean task it is running. Outside a task the door belongs to
the household, whatever position it is in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg.garage_door import GarageControlError, GarageController


class Rig:
    def __init__(self, *, opened=False, task_active=False, state='charging', docked=True):
        self.calls = []
        self.opened = opened
        self.task_active = task_active
        self.state = state
        self.docked = docked
        self.outside = False
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


# --- outside a task, the door is not touched ---------------------------------

def test_a_door_opened_by_hand_is_never_closed():
    """No sequence is running, so nothing in the controller touches the door."""
    rig = Rig(opened=True, task_active=False, state='charging', docked=True)
    controller = rig.build()
    assert controller.task_in_progress is False

    # The controller has no polling entry point at all any more: there is nothing
    # that could notice an open door and decide to close it.
    assert not hasattr(controller, 'update_idle')
    assert not hasattr(controller, 'async_tick')
    assert rig.calls == []


def test_the_controller_offers_no_way_to_close_an_unowned_door():
    """Only a sequence may close. If such an entry point existed, it would be used."""
    rig = Rig(opened=True)
    controller = rig.build()
    for name in ('update_idle', 'async_tick', 'close_if_idle', 'poll'):
        assert not hasattr(controller, name), f'{name}() would be a background closer'


def test_an_open_door_at_startup_is_not_ours():
    """A restart adopts nothing: no sequence means no permission."""
    rig = Rig(opened=True)
    controller = rig.build()
    assert controller.task_in_progress is False
    assert rig.calls == []


# --- inside a task, the integration does close its door ----------------------

def test_starting_a_clean_is_what_grants_permission():
    rig = Rig(opened=False)
    controller = rig.build()
    assert controller.task_in_progress is False

    asyncio.run(controller.open_door())          # the clean is being started
    assert rig.calls == ['open']
    assert controller.task_in_progress is True


def test_the_sequence_finishes_by_closing_its_door():
    rig = Rig(opened=False)
    controller = rig.build()
    asyncio.run(controller.open_door())

    rig.task_active, rig.docked, rig.state = False, True, 'charging'
    assert asyncio.run(controller.sequence_finished()) is True

    assert rig.calls == ['open', 'close']
    assert controller.task_in_progress is False
    assert controller.phase == 'idle'


def test_the_sequence_does_not_finish_while_the_task_runs():
    """Docked with a live task is a recharge visit, not the end of the clean."""
    rig = Rig(opened=False)
    controller = rig.build()
    asyncio.run(controller.open_door())

    rig.task_active, rig.docked, rig.state = True, True, 'charging'
    controller.refresh()
    assert controller.task_running() is True
    assert asyncio.run(controller.sequence_finished()) is False
    assert rig.calls == ['open'], 'a mid-task recharge visit keeps the door open'


def test_the_sequence_reports_finished_once_and_settles():
    rig = Rig(opened=False)
    controller = rig.build()
    asyncio.run(controller.open_door())
    rig.task_active = False
    assert asyncio.run(controller.sequence_finished()) is True
    assert rig.calls == ['open', 'close']
    assert controller.task_in_progress is False

    # Once the sequence is over there is nothing left to advance, and asking again
    # must not command the door a second time.
    assert asyncio.run(controller.sequence_finished()) is False
    assert rig.calls == ['open', 'close']


def test_a_sequence_that_starts_with_the_door_open_still_closes_it():
    """Someone left the door open; the clean still needs it, and closes it after."""
    rig = Rig(opened=True)
    controller = rig.build()
    asyncio.run(controller.open_door())
    assert rig.calls == [], 'no need to command an open door'
    assert controller.task_in_progress is True

    rig.task_active, rig.docked, rig.state = False, True, 'charging'
    assert asyncio.run(controller.sequence_finished()) is True
    assert rig.calls == ['close']


def test_a_hand_closed_door_ends_the_sequence_without_a_command():
    """If the household closed it, that is already the desired end state."""
    rig = Rig(opened=False)
    controller = rig.build()
    asyncio.run(controller.open_door())

    rig.opened = False                 # someone closes it by hand
    rig.task_active, rig.docked, rig.state = False, True, 'charging'
    assert asyncio.run(controller.sequence_finished()) is True
    assert rig.calls == ['open'], 'nothing more to command'
    assert controller.task_in_progress is False


# --- departure closing is a different rule and still works -------------------

def test_departure_closing_still_happens():
    """The robot leaving the zone is a fact about the trip, not a claim on the door."""
    rig = Rig(opened=True, task_active=True, state='cleaning', docked=False)
    rig.outside = True
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


# --- unusable evidence is not a reason to act --------------------------------

def test_unreadable_door_evidence_does_not_close_anything():
    rig = Rig(opened=False)
    controller = rig.build()
    asyncio.run(controller.open_door())

    def broken():
        return {'cover_available': False, 'position': None,
                'open_contact': None, 'close_contact': None}

    controller.door_observation = broken
    rig.task_active, rig.docked, rig.state = False, True, 'charging'
    assert asyncio.run(controller.sequence_finished()) is True
    assert rig.calls == ['open'], 'a blind controller must not move the door'
