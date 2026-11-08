"""The return trip must open the door and leave it open until the robot is in.

Measured 2026-11-08 14:32-14:34, the first real return after a clean:

    14:32:28  returning_home          the clean is over, robot heads back
    14:32:39  paused                  return pause (correct)
    14:32:39  cover opening           door opens (correct)
    14:33:14  phase = outside         open_door() finished
    14:33:16  cover closing           THE BUG: door shut on a robot still outside
    14:33:50  paused                  and paused again, then nothing

Two things went wrong.

1. ``update_returning()`` ended by setting ``phase = 'outside'``, which is the same
   value the controller uses for "a clean is running beyond the door". The very
   next tick, ``update_after_departure()`` saw "robot outside, door open" and shut
   the door -- on a robot that was driving home through it.

2. The second pause had no matching resume. ``_pause_open_close_resume`` releases
   in a ``finally``, but that release was not verified, so a failure there left the
   robot standing still forever.

The same shape appears mid-task: a robot that goes back to charge, empty or wash
during a clean also reports ``returning_home`` while ``task_active`` is still on.
It has to be excluded too, or the door is shut on it for the same reason.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg.garage_door import GarageControlError, GarageController


def controller(*, state, docked=False, outside=True, task_active=False,
               opened=False, close_raises=None, resume_raises=None):
    """A controller whose door and robot the test drives explicitly."""
    box = {"opened": opened, "calls": [], "close_raises": close_raises,
           "resume_raises": resume_raises}

    def door():
        # Polarity as deployed: BOTH contacts read 'off' at their own endpoint
        # (measured 2026-10-14). Reading them as a complementary pair makes the
        # evidence layer report endpoint_conflict and nothing moves.
        return {
            "cover_available": True,
            "position": 100 if box["opened"] else 0,
            "open_contact": "off" if box["opened"] else "on",
            "close_contact": "off" if not box["opened"] else "on",
        }

    box["state"] = state
    box["docked"] = docked
    box["outside"] = outside
    box["task_active"] = task_active

    def robot():
        return NS(state=box["state"], docked=box["docked"],
                  outside_danger_zone=box["outside"],
                  position_trusted=True, fresh=True,
                  task_active=box.get("task_active", task_active),
                  x=25696.0, y=26500.0)

    async def move(direction):
        if direction == "close" and box["close_raises"] is not None:
            raise GarageControlError(box["close_raises"])
        box["calls"].append(direction)
        box["opened"] = direction == "open"

    async def instant(_delay):
        """The pause-confirmation loop sleeps; tests should not."""
        return None

    c = GarageController(
        binding={"open_source": "contact", "close_source": "contact",
                 "open_contact": "binary_sensor.open",
                 "close_contact": "binary_sensor.closed",
                 "open_contact_state": "off", "close_contact_state": "off",
                 "position_settle_seconds": 0, "contact_settle_seconds": 0,
                 "operation_timeout_seconds": 5, "dock_settle_seconds": 1},
        door_observation=door,
        robot_observation=robot,
        move_door=move,
        close_allowed=lambda: True,
        sleep=instant,
    )
    c.refresh()
    return c, box


# --- the return trip ---------------------------------------------------------

def test_a_returning_robot_is_never_closed_out():
    """THE 14:33:16 BUG, at its narrowest.

    The robot is outside the zone (it is driving home through the doorway) and the
    door is open. `update_after_departure` used to treat that as "the clean went
    out, shut the door". Being on the way back must win.
    """
    c, box = controller(state="returning_home", outside=True, opened=True)

    assert c.robot_outside() is True, "the robot really is outside the zone"
    assert c.is_on_the_way_back() is True

    decided = asyncio.run(c.update_after_departure())
    assert decided is False, "the departure rule must not act on a returning robot"
    assert "close" not in box["calls"], "the door must stay open for it"
    assert box["opened"] is True


@pytest.mark.parametrize("state", ["returning_home", "docking", "going_to_wash_the_mop"])
def test_every_returning_state_is_excluded(state):
    c, box = controller(state=state, outside=True, opened=True, task_active=True)
    assert c.is_on_the_way_back() is True
    assert asyncio.run(c.update_after_departure()) is False
    assert "close" not in box["calls"]


def test_a_mid_task_dock_visit_is_excluded_too():
    """Charging, emptying or washing mid-clean: the task is still running.

    The robot sits on its dock inside the zone here, but the exclusion must not
    depend on which side of the zone it happens to report -- the point is that the
    clean is not finished, so the door is not the departure rule's business.
    """
    c, box = controller(state="charging", docked=True, outside=False,
                        task_active=True, opened=True)
    assert c.is_on_the_way_back() is True
    assert asyncio.run(c.update_after_departure()) is False
    assert "close" not in box["calls"]


def test_a_working_robot_outside_still_closes_the_door():
    """The rule this guards must keep working: a robot cleaning beyond the door.

    Removing the close entirely would be the easy over-correction; it would leave
    the door open for the whole clean.
    """
    c, box = controller(state="cleaning", outside=True, opened=True, task_active=True)
    assert c.is_on_the_way_back() is False
    decided = asyncio.run(c.update_after_departure())
    assert decided is True, "a robot working beyond the door must still close it"
    assert c.phase in ("closed_outside", "blocked")


def test_a_return_does_not_leave_the_phase_looking_like_an_exit():
    """`phase = 'outside'` is what made the next tick shut the door.

    After a return the controller must not describe itself with the value that
    means "a clean is running beyond the door".
    """
    c, box = controller(state="returning_home", outside=True, opened=False)

    async def fake_pause():
        # `update_returning` requires the pause to be observed before opening.
        box["state"] = "paused"

    async def fake_resume():
        box["calls"].append("resume")
        box["state"] = "returning_home"

    asyncio.run(c.update_returning(pause=fake_pause, resume=fake_resume))

    assert c.phase != "outside", (
        "the return path must not report the phase that means 'a clean went out'")
    assert "open" in box["calls"]
    assert "resume" in box["calls"]


# --- the unpaired pause ------------------------------------------------------

def test_the_pause_is_waited_for_rather_than_read_once():
    """THE 15:19:49 BUG: one lagging sample was treated as "pause failed".

    The pause command went out, the robot really stopped, and the status sample in
    hand was the one taken *before* it. The code gave up, never opened the door,
    and left the robot paused outside it.

    The fix is to wait for the pause to show up. The order stays pause -> open ->
    resume, because a door told to move while the robot is still driving is a
    collision.
    """
    c, box = controller(state="returning_home", outside=True, opened=False)

    box["state"] = "returning_home"      # the stale reading
    samples = {"n": 0}
    order = []

    async def pause():
        order.append("pause")

    async def resume():
        order.append("resume")

    # Each refresh publishes a newer sample; the robot is paused from the third.
    original_refresh = c.refresh

    def refresh_and_age():
        original_refresh()
        samples["n"] += 1
        if samples["n"] >= 3:
            box["state"] = "paused"

    c.refresh = refresh_and_age

    asyncio.run(c.update_returning(pause=pause, resume=resume))

    assert order == ["pause", "resume"]
    assert box["calls"] == ["open"], "the door opens between pause and resume"
    assert c.phase == "resuming_return"


def test_a_pause_that_never_arrives_neither_opens_nor_resumes():
    """THE HARD RULE: an unconfirmed stop leaves everything where it is.

    No door movement (the robot may still be driving) and no resume (that would
    send it at a door that never opened). The robot waits for a human. Stopping is
    inconvenient; driving into a door is damage, and the two are not exchangeable.
    """
    c, box = controller(state="returning_home", outside=True, opened=False)
    box["state"] = "returning_home"      # never becomes paused

    order = []

    async def pause():
        order.append("pause")

    async def resume():
        order.append("resume")

    with pytest.raises(GarageControlError):
        asyncio.run(c.update_returning(pause=pause, resume=resume))

    assert order == ["pause"], "the robot must not be resumed"
    assert box["calls"] == [], "the door must not move"
    assert c.last_error == "pause_not_observed"
    assert c.phase == "blocked"


def test_the_pause_this_integration_commands_does_not_unlatch_the_return():
    """THE 16:16 LOOP: `paused` is not a returning state, so a snapshot lies.

    The real sequence was:

        16:15:37  returning_home
        16:15:40  cover opening
        16:15:42  paused            <- commanded here
        16:16:16  open confirmed
        16:16:17  cover closing     <- the departure rule, 1.4s later
        16:16:19  returning_home
        16:16:23  paused            <- again
        16:16:51  cover opening     <- and round again

    Between the pause and the resume the robot reads `paused`, which is not in
    RETURNING_STATES, so `is_on_the_way_back()` said no and the departure rule shut
    the door it had just opened. The latch is what survives that gap.
    """
    c, box = controller(state="returning_home", outside=True, opened=True)

    # A return has been seen and opened for.
    c.update_returning  # noqa: B018 - the attribute exists
    c.note_return_latched()

    # Now the robot is paused by this integration, and is parked outside the zone.
    box["state"] = "paused"

    c.refresh()
    assert c.is_returning() is False, "paused is genuinely not a returning state"
    assert c.is_on_the_way_back() is True, (
        "but the return is still in progress, so the door must stay open")
    assert asyncio.run(c.update_after_departure()) is False
    assert "close" not in box["calls"]


def test_the_latch_is_released_once_the_robot_is_on_its_dock():
    """Otherwise the door would never be allowed to shut again."""
    c, box = controller(state="returning_home", outside=False, opened=True)
    c.task_in_progress = True
    c.note_return_latched()

    box["state"] = "charging"
    box["docked"] = True
    box["outside"] = False
    box["task_active"] = False

    c.refresh()
    assert c.is_on_the_way_back() is False, (
        "home and idle: the return is over, the door may close")

    asyncio.run(c.sequence_finished())
    assert c.return_completed is True


def test_a_latched_return_stops_the_departure_rule_for_the_whole_trip():
    """Every pause in the trip, not just the first one."""
    c, box = controller(state="returning_home", outside=True, opened=True)
    c.note_return_latched()

    for state in ("paused", "returning_home", "paused", "docking"):
        box["state"] = state
        c.refresh()
        assert c.is_on_the_way_back() is True, state
        assert asyncio.run(c.update_after_departure()) is False, state
    assert "close" not in box["calls"]


def test_a_failed_pause_in_the_exit_path_also_stays_put():
    """Same rule on the way out: an unconfirmed stop is not released."""
    c, box = controller(state="cleaning", outside=True, opened=True, task_active=True)
    box["state"] = "cleaning"            # the pause never shows up

    paused, resumed = {"n": 0}, {"n": 0}

    async def pause():
        paused["n"] += 1

    async def resume():
        resumed["n"] += 1
        box["calls"].append("resume")

    decided = asyncio.run(c.update_after_departure(pause=pause, resume=resume))

    assert decided is True
    assert paused["n"] == 1
    assert resumed["n"] == 0, "no resume while the stop is unconfirmed"
    assert "close" not in box["calls"], "and no door movement either"
    assert c.last_error == "pause_not_observed"


def test_a_failed_close_still_resumes_the_robot():
    """THE 14:33:50 STUCK PAUSE.

    The robot is paused, the close fails, and the release has to happen anyway --
    a robot left standing still forever is worse than an open door.
    """
    c, box = controller(state="cleaning", outside=True, opened=True,
                        task_active=True, close_raises="close_not_confirmed")
    paused = {"n": 0}
    resumed = {"n": 0}

    async def pause():
        paused["n"] += 1
        box["state"] = "paused"

    async def resume():
        resumed["n"] += 1
        box["calls"].append("resume")

    decided = asyncio.run(c.update_after_departure(pause=pause, resume=resume))

    assert decided is True
    assert paused["n"] == 1
    assert resumed["n"] == 1, "the robot must not be left paused"
    assert c.phase == "blocked"
    assert c.last_error == "close_not_confirmed"


def test_a_failed_resume_is_reported_rather_than_swallowed():
    """If the release itself fails, the caller has to hear about it.

    Swallowing it leaves a paused robot nobody will ever release, with a phase
    that claims the door work is done.
    """
    c, box = controller(state="cleaning", outside=True, opened=True, task_active=True)
    paused = {"n": 0}

    async def pause():
        paused["n"] += 1
        box["state"] = "paused"

    async def resume():
        raise GarageControlError("resume_failed")

    decided = asyncio.run(c.update_after_departure(pause=pause, resume=resume))

    assert decided is True
    assert c.last_error == "resume_failed", (
        "a failed release must be visible, not silently discarded")
    assert c.phase == "blocked"
