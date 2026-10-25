"""An external door close after departure is a courtesy, not a takeover.

``manual_override`` exists so nothing steals the door from a person. But when the
robot has already left the danger zone, a household member shutting the door is
doing what ``after_exit`` was about to do itself. Latching a hold there only
rejects the next schedule: measured 2026-10-25, the following clean failed with
``manual_override`` and the run was stranded for 33 minutes.

The three-way split keeps the guard where it is actually needed -- a robot that
may be shut in -- while not punishing the case it was never meant to cover.

These tests drive ``_classify_external_close()`` directly with a hand-built
observation, because the shared runtime fixtures predate ``after_exit`` and carry
no position at all.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from test_garage_closed_stop import closed_runtime
from test_garage_runtime import runtime_module

ISSUE = "roborock_plus_garage_issue"


def observe(runtime, *, outside, fresh=True, trusted=True, state="cleaning"):
    """Install one robot observation without going through the coordinator."""
    robot = runtime.controller.robot
    runtime.controller.robot = NS(
        now=getattr(robot, "now", 0.0), utc=getattr(robot, "utc", 1000.0),
        state=state, in_cleaning=1, in_returning=0,
        summary=getattr(robot, "summary", {}),
        position_trusted=trusted, docked=False, fresh=fresh,
        sample_id=getattr(robot, "sample_id", 1),
        outside_danger_zone=outside,
        position_sample_id=getattr(robot, "position_sample_id", 1),
    )
    runtime.controller.phase = "task_open"
    runtime.session.active = True
    runtime.session.started = True
    runtime.session.session_id = "session-1"


def issue_reasons(boundaries):
    return [data.get("reason") for name, data in boundaries.events if name == ISSUE]


# --- confirmed outside: record, do not hold ---------------------------------

def test_a_close_after_departure_is_recorded_without_a_hold():
    boundaries, runtime = closed_runtime()
    observe(runtime, outside=True)
    runtime._classify_external_close()
    assert runtime.controller.reason is None, (
        "the robot is already outside; shutting the door is what after_exit does"
    )


def test_a_close_after_departure_publishes_the_reason():
    """Silence would hide the event; the household needs to see it happened."""
    boundaries, runtime = closed_runtime()
    observe(runtime, outside=True)
    runtime._classify_external_close()
    assert "external_close_after_exit" in issue_reasons(boundaries)


def test_the_next_clean_is_not_blocked_after_an_external_close():
    """The whole point: one manual close must not strand the next schedule."""
    boundaries, runtime = closed_runtime()
    observe(runtime, outside=True)
    runtime._classify_external_close()
    assert runtime.controller.reason is None
    asyncio.run(_expect_no_manual_override(runtime))


async def _expect_no_manual_override(runtime):
    async def send():
        return "sent"
    try:
        await runtime.controller.async_execute("app_start", send, new_task=True)
    except Exception as err:  # noqa: BLE001
        assert "manual_override" not in str(err), (
            f"the hold survived the external close: {err}"
        )


# --- confirmed inside: still hold -------------------------------------------

def test_a_close_with_the_robot_inside_still_holds():
    """The guard's actual job: a robot may be shut in and unable to get out."""
    boundaries, runtime = closed_runtime()
    observe(runtime, outside=False)
    runtime._classify_external_close()
    assert runtime.controller.reason == "manual_override"
    assert runtime.controller.phase == "manual_hold"


# --- unknown position: still hold -------------------------------------------

def test_a_close_with_an_unknown_position_still_holds():
    """Not knowing where the robot is must not fail open."""
    boundaries, runtime = closed_runtime()
    observe(runtime, outside=None)
    runtime._classify_external_close()
    assert runtime.controller.reason == "manual_override"


@pytest.mark.parametrize("flag", ["fresh", "trusted"])
def test_a_close_with_an_untrusted_position_still_holds(flag):
    boundaries, runtime = closed_runtime()
    observe(runtime, outside=True,
            fresh=flag != "fresh", trusted=flag != "trusted")
    runtime._classify_external_close()
    assert runtime.controller.reason == "manual_override", (
        f"a position that cannot be trusted ({flag} false) must not authorise "
        f"leaving the door shut"
    )


# --- event de-duplication ---------------------------------------------------

def test_the_reason_is_not_republished_on_every_observation():
    boundaries, runtime = closed_runtime()
    observe(runtime, outside=True)
    runtime._classify_external_close()
    first = len(issue_reasons(boundaries))
    runtime._classify_external_close()
    second = len(issue_reasons(boundaries))
    assert second == first, "the (session_id, reason) key must still de-duplicate"


def test_an_inactive_session_is_untouched():
    """The classification runs inside the task-phase branch only; an idle
    controller keeps its existing behaviour."""
    boundaries, runtime = closed_runtime()
    observe(runtime, outside=True)
    runtime.session.active = False
    assert runtime.controller.reason is None
