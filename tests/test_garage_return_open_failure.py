"""A door that will not open must stop the return AND be reported.

Measured 2026-11-12, the first afternoon clean after v0.12.2:

    15:06:44  returning_home    the clean ended, robot heads back
    15:06:45  cover opening     the return opened the door
    15:06:46  paused            the return pause
    15:07:31  resuming_return   the return resumed
    15:07:50  open contact stops reading "fully open" (the door was stopped)
    15:07:53  paused            the robot is paused a second time
    15:09:22  phase = opening   the sequence tries to reopen, and then...
    16:15:31  cover closed      ...nothing for 66 minutes
    16:29:14  returning_home    the household pressed "return to base" by hand

The robot stood in front of a closed door for 81 minutes, and the household found
it by accident.

The safety rule is unchanged and was never the bug: **a door that is not
confirmed open must not be followed by a resume.** AGENTS.md spells it out --
"门如果没有打开，就不要 resume，发出警告" -- and the robot standing still is the
correct, intended outcome of that rule.

What was broken is the second half of that same sentence: **发出警告**. Nothing
warned. Three separate reasons, all measured:

1. `open_door()`'s `except GarageControlError: raise` dropped the reason, so
   `last_error` stayed `None` and `garage_problem` read "off".
2. `update_returning` left `phase='opening'` -- not `blocked` -- so even the
   phase said "mid-operation", not "stuck".
3. `_finish_sequence` caught the error and logged at `info`; `notify()` was never
   called, so `roborock_plus_garage_issue` never fired and the notify automation
   had nothing to react to.

These tests pin the warning, not a resume.
"""

from __future__ import annotations

import asyncio

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg.garage_door import GarageControlError

from test_controlled_exit_runtime import build, run
from test_garage_return_trip import controller


def test_a_door_that_will_not_open_stops_the_robot_and_says_why() -> None:
    """Pause confirmed, door never opens: no resume, but a recorded reason.

    The robot must stay put -- that is the safety rule -- and `last_error` must
    carry the reason so the problem sensor lights and the caller can announce it.
    """
    c, box = controller(state="returning_home", outside=True, opened=False)
    order: list[str] = []

    async def pause() -> None:
        order.append("pause")
        box["state"] = "paused"

    async def resume() -> None:
        order.append("resume")
        box["state"] = "returning_home"

    async def open_fails(_direction: str) -> None:
        raise GarageControlError("open_not_confirmed")

    c.move_door = open_fails

    with pytest.raises(GarageControlError):
        asyncio.run(c.update_returning(pause=pause, resume=resume))

    assert order == ["pause"], (
        "the door is not confirmed open, so the robot must NOT be resumed: "
        "driving at a shut door is the one failure this project will not accept")
    assert box["state"] == "paused", "the robot stays stopped and waits for a human"
    assert c.phase == "blocked", (
        "`opening` reads as 'mid-operation'; `blocked` is what makes "
        "garage_problem light up")
    assert c.last_error == "open_not_confirmed", (
        "the reason must survive: without it garage_problem reports 'off'")


def test_a_door_that_cannot_be_read_stops_the_robot_and_says_why() -> None:
    """The same rule for an unreadable door rather than a refused one."""
    c, box = controller(state="returning_home", outside=True, opened=False)
    order: list[str] = []

    async def pause() -> None:
        order.append("pause")
        box["state"] = "paused"

    async def resume() -> None:
        order.append("resume")

    c.door_blocked_reason = lambda: "contacts_unreadable"  # type: ignore[assignment]

    with pytest.raises(GarageControlError):
        asyncio.run(c.update_returning(pause=pause, resume=resume))

    assert order == ["pause"], "a blind door must never be followed by a resume"
    assert c.phase == "blocked"
    assert c.last_error == "contacts_unreadable"


def test_the_unconfirmed_pause_rule_still_stands() -> None:
    """The hard rule from AGENTS.md, unchanged by any of the above.

    An unconfirmed stop may still be a moving robot; resuming would send it at a
    door that never opened.
    """
    c, box = controller(state="returning_home", outside=True, opened=False)
    order: list[str] = []
    box["state"] = "returning_home"  # never becomes paused

    async def pause() -> None:
        order.append("pause")

    async def resume() -> None:
        order.append("resume")

    with pytest.raises(GarageControlError):
        asyncio.run(c.update_returning(pause=pause, resume=resume))

    assert order == ["pause"]
    assert box["calls"] == [], "the door must not move"
    assert c.last_error == "pause_not_observed"
    assert c.phase == "blocked"


# --- the announcement -------------------------------------------------------

def test_a_held_return_is_announced_and_does_not_spam(monkeypatch) -> None:
    """THE 81-MINUTE SILENCE: `_handle_return` logged at info and carried on.

    The robot is stopped -- correct -- but the hold has to reach the household.
    `notify` deduplicates on the reason, so a loop that keeps retrying must
    announce once, not once per second.

    This drives `_handle_return` (the sequence's own entry point for a returning
    robot) rather than `_finish_sequence`, so the test does not have to script the
    loop's timing and still exercises the real path that stayed silent.
    """
    from _garage_pkg.garage_runtime import EVENT_GARAGE_ISSUE

    runtime, coordinator, door, services = build()

    fired: list[tuple[str, dict]] = []
    runtime.hass.bus.async_fire = lambda event, data: fired.append((event, data))

    coordinator.observe(state="returning_home", x=25696.0, y=24000.0)

    async def open_fails(_direction: str) -> None:
        raise GarageControlError("open_not_confirmed")

    runtime.controller.move_door = open_fails

    async def confirm() -> bool:
        return True

    runtime.controller._confirm_pause = confirm

    async def no_sleep(_seconds: float) -> None:
        return None

    runtime.controller.sleep = no_sleep

    async def pause() -> None:
        coordinator.observe(state="paused", x=25696.0, y=24000.0)

    async def resume() -> None:  # pragma: no cover - must never run
        raise AssertionError("a shut door must never be followed by a resume")

    runtime.async_control_command = lambda name, params=None: (
        pause() if name == "app_pause" else resume()
    )

    run(runtime._handle_return())
    run(runtime._handle_return())
    run(runtime._handle_return())

    matching = [data for event, data in fired if event == EVENT_GARAGE_ISSUE]
    assert matching, "a held return must fire the issue event"
    assert matching[0]["reason"] == "open_not_confirmed"
    assert matching[0]["active"] is True
    assert len(matching) == 1, (
        "the loop retries every second; the same hold must be announced once")
