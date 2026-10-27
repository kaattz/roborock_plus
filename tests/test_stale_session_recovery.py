"""A finished session with an unusable record must not block forever.

Live trace (2026-10-27 11:56, and again 2026-10-26 15:28):

    session 369f2e6...   left by the interrupted clean on 2026-10-25
    active=False  started=True  cancel_requested=True  correlation_valid=False
    robot charging, door closed, no task
    garage_control.py -> fault('session_recovery_required')

and then the reset meant to clear it:

    reset() -> raise GarageControlError('session_recovery_required')

The same fact both raised the block and refused the cure, so reset was useless
and the fault returned on the next tick. These tests pin the distinction that
fixes it, and pin the guard that must NOT be weakened: a broken record beside a
running task is still a hard stop.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from test_garage_runtime import Boundaries, binding


def rig(*, correlation_valid=False, active=False, started=True,
        cancel_requested=True, docked=True, known_idle=True):
    b = Boundaries(initialized=True)
    r = b.create()
    r._restored = True
    r._first_install = False
    controller = r.controller
    controller.binding = dict(controller.binding, task_door_mode='after_exit')
    controller.phase = 'idle'
    controller.reason = None
    controller._user_hold = False

    session = r.session
    session.session_id = 'stale-session'
    session.started = started
    session.active = active
    session.known_idle = known_idle
    session._cancel_requested = cancel_requested
    session._correlation_invalid = not correlation_valid
    session.reason = 'session_recovery_required' if not correlation_valid else None

    def robot_observation():
        return NS(
            now=b.now, utc=b.utc, state='charging', in_cleaning=0,
            in_returning=0, summary=b.summary, position_trusted=True,
            docked=docked, fresh=True, sample_id=1,
            outside_danger_zone=False, position_sample_id=1,
            position=NS(x=25696.0, y=28439.0),
        )

    controller.robot_observation = robot_observation
    controller.robot = robot_observation()
    controller.door_observation = lambda: {
        'cover_available': True, 'restored': False, 'position': 0,
        'open_contact': 'off', 'close_contact': 'on',
    }
    controller.close_allowed = lambda: True
    controller._preflight = lambda direction: None

    async def persist():
        return None

    controller.persist = persist
    return b, r, controller


# --- the deadlock -----------------------------------------------------------

def test_an_idle_docked_robot_can_be_reset_despite_a_broken_record():
    """The case the household hit twice: nothing running, but reset refused."""
    b, r, controller = rig()
    controller.reset()
    assert controller.reason is None
    assert controller.phase == 'idle'


def test_reset_discards_the_unusable_session():
    b, r, controller = rig()
    controller.reset()
    assert r.session.session_id is None, 'the stale record must be replaced'
    assert r.session.started is False
    assert r.session._correlation_invalid is False


def test_reset_clears_a_broken_record_that_never_marked_itself_idle():
    """The exact live failure on 2026-10-27.

    The stored record had ``known_idle=False`` as well as a broken correlation.
    Requiring idleness in ``reset_to_idle`` therefore refused, and the guard just
    below it refused for the same reason -- so reset raised the very error the
    notification told the user to clear. Reproduced here so it cannot come back.
    """
    b, r, controller = rig(known_idle=False)
    controller.reset()
    assert controller.reason is None
    assert r.session.session_id is None
    assert r.session.known_idle is True
    assert controller.phase == 'idle'


def test_a_tick_does_not_fault_an_idle_docked_robot():
    """The tick must not re-raise the fault it just cleared."""
    b, r, controller = rig()
    asyncio.run(controller.async_update())
    assert controller.reason is None
    assert r.session.session_id is None


def test_the_tick_clears_the_session_so_the_fault_cannot_return():
    b, r, controller = rig()
    asyncio.run(controller.async_update())
    asyncio.run(controller.async_update())
    assert controller.reason is None


# --- the guard that must not be weakened ------------------------------------

def test_an_active_session_with_a_broken_record_still_faults():
    """A broken record beside a running task is exactly the hazard the flag
    exists for: the controller cannot tell which task it is opening for."""
    b, r, controller = rig(active=True, docked=False)
    asyncio.run(controller.async_update())
    assert controller.reason == 'session_recovery_required'
    assert controller.phase == 'blocked'


def test_an_active_session_is_in_play_even_sitting_on_its_dock():
    """The ``active`` clause must be load-bearing on its own.

    A task can be recorded as running while the robot is momentarily docked
    (docking, washing, charging mid-task). Deciding "in play" from the dock
    alone would let that task's record be discarded, losing the ability to say
    "end this task before closing the door".
    """
    b, r, controller = rig(active=True, docked=True, known_idle=False)
    assert controller._session_still_in_play() is True
    asyncio.run(controller.async_update())
    assert controller.reason == 'session_recovery_required'
    assert r.session.session_id == 'stale-session', (
        'an active task must never be discarded, even while docked')


def test_reset_still_refuses_while_a_task_is_active():
    b, r, controller = rig(active=True, docked=False)
    with pytest.raises(Exception) as err:
        controller.reset()
    assert 'session_recovery_required' in str(getattr(err.value, 'reason', err.value))
    assert r.session.session_id == 'stale-session', 'a live task must not be discarded'


def test_a_robot_away_from_its_dock_is_treated_as_in_play():
    """Parked is part of the remedy: a robot that is not docked may still be
    executing something we cannot describe."""
    b, r, controller = rig(active=False, docked=False)
    asyncio.run(controller.async_update())
    assert controller.reason == 'session_recovery_required'


def test_a_session_that_never_started_still_needs_a_human():
    """A revoked record before the robot ever moved is not history.

    That shape means something took the task over while the controller was not
    looking -- typically an external task. We cannot know what the robot actually
    did, so the record must be kept and a human must reconcile it. This is the
    case ``test_external_task_revokes_verified_idle_across_quiet_dock_samples``
    pins, and it must stay refused even though the robot is docked.
    """
    b, r, controller = rig(started=False)
    assert controller._session_still_in_play() is True
    asyncio.run(controller.async_update())
    assert controller.reason == 'session_recovery_required'
    with pytest.raises(Exception) as err:
        controller.reset()
    assert 'session_recovery_required' in str(getattr(err.value, 'reason', err.value))


def test_reset_refuses_when_the_robot_is_not_docked():
    b, r, controller = rig(active=False, docked=False)
    with pytest.raises(Exception) as err:
        controller.reset()
    assert 'session_recovery_required' in str(getattr(err.value, 'reason', err.value))


# --- a healthy session is untouched -----------------------------------------

def test_a_valid_session_survives_a_tick():
    b, r, controller = rig(correlation_valid=True, cancel_requested=False)
    before = r.session.session_id
    asyncio.run(controller.async_update())
    assert r.session.session_id == before
    assert controller.reason is None


def test_reset_leaves_a_valid_session_alone():
    b, r, controller = rig(correlation_valid=True, cancel_requested=False)
    controller.reset()
    assert r.session.session_id == 'stale-session'


# --- the session object itself ----------------------------------------------

def test_reset_to_idle_refuses_an_active_task():
    b, r, _controller = rig(active=True)
    with pytest.raises(Exception):
        r.session.reset_to_idle()


def test_reset_to_idle_accepts_a_record_that_never_established_idleness():
    """The exact live shape: a broken record also failed to mark itself idle.

    Demanding ``known_idle`` here made the method unreachable for precisely the
    records it exists to clear, so the fault could never be removed.
    """
    b, r, _controller = rig(known_idle=False)
    r.session.reset_to_idle()
    assert r.session.session_id is None
    assert r.session.known_idle is True, 'a cleared record must be usable again'
    assert r.session._correlation_invalid is False


def test_a_cleared_session_must_survive_persistence():
    """The bug that made v0.9.3 look broken: the record could not be saved.

    ``normalize_session_payload`` requires ``baseline_records`` to be a LIST.
    An empty tuple passed every in-memory check but made ``async_save`` raise
    ``session_recovery_required``, so the cleaned state never reached disk: the
    stale record was restored on the next load and the fault came back within a
    minute of a successful reset.

    Persisting is the only thing that makes the fix stick, so it is asserted
    directly rather than inferred from the in-memory state.
    """
    from test_garage_runtime import runtime_module

    # The session module is loaded by the shared fixture harness under a
    # synthetic package name, so resolve it the same way the other tests do.
    b, r, _controller = rig(known_idle=False)
    r.session.reset_to_idle()
    payload = r.session.to_dict()
    task_module = __import__(type(r.session).__module__, fromlist=['normalize_session_payload'])
    normalized = task_module.normalize_session_payload(payload)
    assert normalized['session_id'] is None
    assert normalized['baseline_records'] == []
    assert normalized['known_idle'] is True
