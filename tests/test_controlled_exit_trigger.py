"""When does a clean command get an exit run in front of it, and when not.

Two separate gates are covered here because they answer different questions:

* ``should_controlled_exit`` -- is this the start of a brand-new clean?
* ``_robot_is_outside``      -- may the clean command skip the passage check?

Getting the first wrong means a needless physical detour. Getting the second
wrong means waving a robot through a shut door, so its clauses are tested one at
a time.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from test_garage_runtime import Boundaries, binding


def runtime_with(**overrides):
    b = Boundaries(initialized=True)
    b.entry.options['garage_bindings']['robot'] = dict(binding(), **overrides)
    r = b.create()
    r._restored = True
    r._first_install = False
    # create() takes the binding by value; apply the overrides to the live copy.
    r.binding = dict(r.binding, **overrides)
    r.controller.binding = r.binding
    r.controller.phase = 'idle'
    r.controller.reason = None
    return b, r


ENABLED = dict(
    controlled_exit_enabled=True,
    controlled_exit_x=25696,
    controlled_exit_y=26500,
    controlled_exit_timeout_seconds=120,
)


# --- gate 1: should this command be preceded by an exit run? ----------------

def test_the_feature_off_never_triggers():
    b, r = runtime_with()
    for name in ('app_start', 'app_segment_clean', 'app_zoned_clean', 'app_spot'):
        assert r._should_controlled_exit(name) is False


@pytest.mark.parametrize("name", [
    'app_start', 'app_segment_clean', 'app_zoned_clean', 'app_spot',
])
def test_every_real_clean_command_is_covered(name):
    """The household's automations use clean_area -> app_segment_clean, so
    covering only app_start would silently do nothing for them."""
    b, r = runtime_with(**ENABLED)
    assert r._should_controlled_exit(name) is True


def test_goto_never_triggers_its_own_exit_run():
    """app_goto_target is the vehicle; treating it as a clean would recurse."""
    b, r = runtime_with(**ENABLED)
    assert r._should_controlled_exit('app_goto_target') is False


def test_resume_never_triggers():
    b, r = runtime_with(**ENABLED)
    assert r._should_controlled_exit('app_resume') is False


def test_an_active_session_never_triggers():
    """Mid-task: a wash-and-resume keeps the session, and a manual restart while
    the old session is live would collide with task_already_active."""
    b, r = runtime_with(**ENABLED)
    r.session.begin(b.summary, b.utc, 'app_start')
    assert r.session.active is True
    assert r._should_controlled_exit('app_start') is False


@pytest.mark.parametrize("phase", ['task_open', 'returning', 'docked_intermission', 'manual_hold'])
def test_only_idle_or_outside_closed_phases_trigger(phase):
    b, r = runtime_with(**ENABLED)
    r.controller.phase = phase
    assert r._should_controlled_exit('app_start') is False


def test_an_existing_fault_never_triggers():
    b, r = runtime_with(**ENABLED)
    r.controller.reason = 'manual_override'
    assert r._should_controlled_exit('app_start') is False


# --- gate 2: may a clean command skip the passage check? --------------------

def controller_with(outside=None, *, trusted=True, fresh=True, docked=False):
    b, r = runtime_with()
    c = r.controller
    c.robot = NS(
        now=b.now, utc=b.utc, state='idle', in_cleaning=0, in_returning=0,
        summary=b.summary, position_trusted=trusted, docked=docked, fresh=fresh,
        sample_id=1, outside_danger_zone=outside, position_sample_id=1,
    )
    return c


def test_a_confirmed_outside_robot_may_skip_passage():
    assert controller_with(True)._robot_is_outside() is True


@pytest.mark.parametrize("outside", [False, None])
def test_an_inside_or_unknown_robot_must_wait_for_passage(outside):
    assert controller_with(outside)._robot_is_outside() is False


def test_a_stale_robot_must_wait_for_passage():
    assert controller_with(True, fresh=False)._robot_is_outside() is False


def test_an_untrusted_position_must_wait_for_passage():
    assert controller_with(True, trusted=False)._robot_is_outside() is False


def test_a_docked_robot_is_never_outside():
    """A dock-substituted coordinate must not masquerade as a departure."""
    assert controller_with(True, docked=True)._robot_is_outside() is False


def test_the_skip_requires_exactly_true_not_truthy():
    """``outside_danger_zone`` is Optional[bool]; a truthy stand-in must not do."""
    assert controller_with('yes')._robot_is_outside() is False
