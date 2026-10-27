"""The position-driven door rules, tested without Home Assistant.

Two properties matter more than any other here:

1. opening is proactive and confirmed before the robot is released;
2. nothing latches. Unusable feedback makes the controller stand down, and the
   next usable sample resumes normal work with no operator step.

The second is the reason this module exists: the previous design required a
human to "release" a controller that had latched on a stale task record, and the
release itself was blocked by that same record.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg.garage_door import GarageControlError, GarageController


class Rig:
    """A door and a robot, both scriptable."""

    def __init__(self, *, open_source='contact', close_source='contact',
                 usable=True, opened=False):
        self.calls = []
        self.open_source = open_source
        self.close_source = close_source
        self.usable = usable
        self.opened = opened
        self.contradictory = False
        self.outside = False
        self.docked = True
        self.state = 'charging'
        self.trusted = True
        self.fresh = True
        self.permit_close = True

    def door(self):
        if not self.usable:
            return {'cover_available': False, 'position': None,
                    'open_contact': None, 'close_contact': None}
        if self.contradictory:
            return {'cover_available': True, 'position': None,
                    'open_contact': 'on', 'close_contact': 'on'}
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
        )

    async def move(self, direction):
        self.calls.append(direction)
        self.opened = direction == 'open'
        return {'ok': True}

    def build(self):
        return GarageController(
            binding={
                'open_source': self.open_source,
                'close_source': self.close_source,
                'open_contact': 'binary_sensor.door_open',
                'close_contact': 'binary_sensor.door_closed',
                'open_contact_state': 'on',
                'close_contact_state': 'on',
                'contact_settle_seconds': 0,
                'open_position': 95,
                'close_position': 5,
                'operation_timeout_seconds': 5,
            },
            door_observation=self.door,
            robot_observation=self.robot,
            move_door=self.move,
            close_allowed=lambda: self.permit_close,
            clock=lambda: 0.0,
        )


# --- proactive opening -------------------------------------------------------

def test_opening_is_confirmed_before_the_caller_may_release_the_robot():
    rig = Rig(opened=False)
    controller = rig.build()
    asyncio.run(controller.open_door())
    assert rig.calls == ['open']
    assert controller.open_confirmed() is True
    assert controller.phase == 'outside'


def test_an_already_open_door_costs_no_command():
    rig = Rig(opened=True)
    controller = rig.build()
    asyncio.run(controller.open_door())
    assert rig.calls == []
    assert controller.phase == 'outside'


def test_opening_with_unusable_feedback_never_moves_the_door():
    rig = Rig(usable=False)
    controller = rig.build()
    with pytest.raises(GarageControlError) as err:
        asyncio.run(controller.open_door())
    assert err.value.reason == 'feedback_unavailable'
    assert rig.calls == []
    assert controller.phase == 'blocked'


# --- the no-latch property ---------------------------------------------------

def test_a_blocked_controller_recovers_by_itself():
    """The whole point: no operator step between "blocked" and normal work."""
    rig = Rig(usable=False)
    controller = rig.build()
    with pytest.raises(GarageControlError):
        asyncio.run(controller.open_door())
    assert controller.phase == 'blocked'

    rig.usable = True
    asyncio.run(controller.open_door())
    assert controller.phase == 'outside'
    assert controller.last_error is None
    assert rig.calls == ['open']


def test_repeated_blocked_attempts_never_accumulate_state():
    rig = Rig(usable=False)
    controller = rig.build()
    for _ in range(5):
        with pytest.raises(GarageControlError):
            asyncio.run(controller.open_door())
    assert controller.phase == 'blocked'
    rig.usable = True
    asyncio.run(controller.open_door())
    assert rig.calls == ['open']


def test_there_is_no_reset_entry_at_all():
    """If the module offers a way to "release" something, the latch is back."""
    rig = Rig()
    controller = rig.build()
    for attr in ('reset', 'clear_fault', 'release', 'acknowledge'):
        assert not hasattr(controller, attr), (
            f'{attr}() would be a manual latch release; the design has no latches')


# --- reactive closing --------------------------------------------------------

def test_closing_happens_only_after_the_robot_is_confirmed_outside():
    rig = Rig(opened=True)
    controller = rig.build()
    rig.outside = False
    assert asyncio.run(controller.update_after_departure()) is False
    assert rig.calls == [], 'a robot still inside must not have the door shut on it'


def test_closing_happens_once_the_robot_is_outside():
    rig = Rig(opened=True)
    controller = rig.build()
    rig.outside = True
    rig.docked = False
    assert asyncio.run(controller.update_after_departure()) is True
    assert rig.calls == ['close']
    assert controller.phase == 'closed_outside'


def test_an_untrusted_position_does_not_authorise_closing():
    rig = Rig(opened=True)
    controller = rig.build()
    rig.outside = True
    rig.docked = False
    rig.trusted = False
    assert asyncio.run(controller.update_after_departure()) is False
    assert rig.calls == []


def test_a_docked_robot_never_counts_as_outside_even_if_the_flag_says_so():
    """``docked`` and ``outside`` contradict; the safe reading is "still in".

    A coordinate that claims the robot is past the zone while the dock reports it
    parked is a contradiction, not a departure. Closing on it would shut the door
    on a robot that is physically on its charger inside the cabinet.
    """
    rig = Rig(opened=True)
    controller = rig.build()
    rig.outside = True      # flag says outside
    rig.docked = True       # but the robot is on its dock
    assert controller.robot_outside() is False
    assert asyncio.run(controller.update_after_departure()) is False
    assert rig.calls == []


def test_the_family_permission_blocks_closing():
    rig = Rig(opened=True)
    rig.permit_close = False
    controller = rig.build()
    rig.outside = True
    rig.docked = False
    with pytest.raises(GarageControlError) as err:
        asyncio.run(controller.update_after_departure())
    assert err.value.reason == 'close_not_allowed'
    assert rig.calls == []


# --- returning: pause, open, resume ------------------------------------------

def test_returning_pauses_before_opening_and_resumes_only_after():
    rig = Rig(opened=False)
    controller = rig.build()
    rig.state, rig.docked = 'returning_home', False
    order = []

    async def pause():
        order.append('pause')
        rig.state = 'paused'

    async def resume():
        order.append('resume')

    asyncio.run(controller.update_returning(pause=pause, resume=resume))
    assert order == ['pause', 'resume']
    assert rig.calls == ['open'], 'the door must open between pause and resume'


def test_resuming_without_an_observed_pause_is_refused():
    """Resuming on an unconfirmed pause would drive the robot at a moving door."""
    rig = Rig(opened=False)
    controller = rig.build()
    rig.state, rig.docked = 'returning_home', False
    order = []

    async def pause():
        order.append('pause')

    async def resume():
        order.append('resume')

    with pytest.raises(GarageControlError) as err:
        asyncio.run(controller.update_returning(pause=pause, resume=resume))
    assert err.value.reason == 'pause_not_observed'
    assert order == ['pause'], 'must not resume'
    assert rig.calls == [], 'must not open before the pause is real'


def test_an_already_open_door_needs_no_pause_on_return():
    rig = Rig(opened=True)
    controller = rig.build()
    rig.state, rig.docked = 'returning_home', False
    order = []

    async def pause():
        order.append('pause')

    async def resume():
        order.append('resume')

    asyncio.run(controller.update_returning(pause=pause, resume=resume))
    assert order == []


# --- final close -------------------------------------------------------------

def test_the_door_is_shut_once_the_robot_is_docked_and_idle():
    rig = Rig(opened=True)
    controller = rig.build()
    rig.state, rig.docked = 'charging', True
    asyncio.run(controller.update_idle())
    assert rig.calls == ['close']
    assert controller.phase == 'idle'


def test_a_running_task_keeps_the_door_alone():
    rig = Rig(opened=True)
    controller = rig.build()
    rig.state, rig.docked = 'cleaning', False
    asyncio.run(controller.update_idle())
    assert rig.calls == []


@pytest.mark.parametrize("state", [
    'cleaning', 'segment_cleaning', 'zoned_cleaning', 'spot_cleaning',
    'returning_home', 'going_to_wash_the_mop', 'paused', 'starting',
])
def test_no_running_or_moving_state_ever_gets_the_door_shut(state):
    """The final close is only for a task that is over.

    ``update_idle`` runs on every tick, so any state it fails to recognise as
    "still busy" would shut the door mid-task. Enumerated rather than sampled:
    one forgotten state is a robot trapped outside.
    """
    rig = Rig(opened=True)
    controller = rig.build()
    rig.state = state
    rig.docked = state in ('paused',)   # paused can be docked mid-task
    asyncio.run(controller.update_idle())
    assert rig.calls == [], f'{state} is still part of a task; the door must stay open'


# --- evidence sources --------------------------------------------------------

def test_a_timed_source_never_claims_confirmation():
    """``timed`` has no endpoint, so it must never report "confirmed".

    Claiming a timed door is open would release the robot at a door only a
    stopwatch believes in.
    """
    rig = Rig(open_source='timed')
    controller = rig.build()
    assert controller.open_confirmed() is False


def test_a_position_source_uses_the_threshold():
    """A position endpoint is confirmed only past its configured threshold."""
    rig = Rig(open_source='position')
    rig.opened = True
    controller = GarageController(
        binding={
            'open_source': 'position',
            'close_source': 'contact',
            'open_contact': 'binary_sensor.door_open',
            'close_contact': 'binary_sensor.door_closed',
            'open_contact_state': 'on',
            'close_contact_state': 'on',
            'contact_settle_seconds': 0,
            'position_kind': 'measured',
            'position_settle_seconds': 0,
            'open_position': 95,
            'close_position': 5,
            'operation_timeout_seconds': 5,
        },
        door_observation=rig.door,
        robot_observation=rig.robot,
        move_door=rig.move,
        close_allowed=lambda: rig.permit_close,
        clock=lambda: 0.0,
    )
    assert controller.open_confirmed() is True
    rig.opened = False
    controller.refresh()
    assert controller.open_confirmed() is False


def test_contradictory_contacts_are_not_usable():
    """Both sensors claiming the same endpoint means the evidence is broken.

    A door reported as simultaneously fully open and fully closed cannot be read;
    acting on it would release the robot on a contradiction.
    """
    rig = Rig()
    controller = rig.build()
    rig.contradictory = True
    controller.refresh()
    assert controller.door_blocked_reason() == 'endpoint_conflict'
