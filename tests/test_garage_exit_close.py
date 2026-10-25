"""The 2026-10-25 defect: one missed position sample cancelled a real departure.

Live evidence on 2026-10-25, after_exit mode:

    15:54:52.8  open_cover (integration) -> door opens
    15:55:41.8  robot starts cleaning; outside_danger_zone -> on   (departure)
    15:55:48    outside_danger_zone -> unknown (lasts 4.9s)
    15:55..16:06  on -> unknown -> on, sixteen times; unknown lasts 1.0-1.3s
    15:58:36.3  integration faults: feedback_unavailable (previous_phase=task_open)
    15:58:39.9  door closed -- no service call, shut by hand

The Logbook has no ``close_cover`` between 15:55:41 and 15:58:39: the
integration never tried. ``refresh()`` folded "this poll read no position" into
"the position cannot be trusted" and cleared ``_departure_armed``. Because
``unknown`` arrives roughly every 30s and lasts about a second, that branch was
hit essentially every trip, so after_exit could not close on its own.

The tests below pin the distinction in both directions: ``None`` must not
advance, rewind, or be mistaken for evidence, while genuinely untrusted input
must still drop the arming.
"""

from __future__ import annotations

import asyncio

import pytest

from test_garage_after_exit import ExitReplay


def _departure_state(replay):
    return (
        replay.controller._departure_armed,
        replay.controller._exit_due,
        replay.controller._position_sample,
    )


def armed_and_inside() -> ExitReplay:
    """A controller that has seen the robot inside on this trip."""
    replay = ExitReplay()
    replay.handover()
    asyncio.run(replay.start())
    replay.outside = False
    asyncio.run(replay.poll(state="cleaning", cleaning=1, docked=False))
    assert replay.controller._departure_armed is True
    return replay


# --- the fix: a missed sample is inert ---------------------------------------

def test_a_missed_sample_changes_nothing():
    """``unknown`` is not "inside" and not "outside"; it must be a no-op."""
    replay = armed_and_inside()
    before = _departure_state(replay)
    replay.outside = None
    asyncio.run(replay.poll())
    after = _departure_state(replay)
    assert after == before, (
        "a poll that read no position must leave arming, exit_due and the sample "
        "token untouched; clearing them is how the 2026-10-25 door stayed open"
    )


def test_a_missed_sample_does_not_close_by_itself():
    replay = armed_and_inside()
    replay.outside = None
    asyncio.run(replay.poll())
    assert not replay.closes(), "no position was read, so no departure was observed"


def test_a_missed_sample_between_inside_and_outside_still_closes():
    """THE BUG: inside -> unknown -> outside must close, not silently give up."""
    replay = armed_and_inside()
    replay.outside = None
    asyncio.run(replay.poll())
    replay.outside = True
    asyncio.run(replay.poll())
    assert len(replay.closes()) == 1


def test_several_consecutive_missed_samples_still_close():
    """The live system produced a 37s gap between usable samples."""
    replay = armed_and_inside()
    for _ in range(3):
        replay.outside = None
        asyncio.run(replay.poll())
    replay.outside = True
    asyncio.run(replay.poll())
    assert len(replay.closes()) == 1


def test_a_missed_sample_does_not_fire_a_second_close():
    replay = armed_and_inside()
    replay.outside = True
    asyncio.run(replay.poll())
    assert len(replay.closes()) == 1
    replay.outside = None
    asyncio.run(replay.poll())
    replay.outside = True
    asyncio.run(replay.poll())
    assert len(replay.closes()) == 1, "the trip already closed; a missed sample must not rearm"


def test_the_exit_due_flag_survives_a_missed_sample():
    """If the crossing was already seen, a later gap must not cancel the close."""
    replay = armed_and_inside()
    replay.outside = True
    replay.advance(1)                     # a new sample token arrives with the reading
    replay.controller.refresh()
    assert replay.controller._exit_due is True
    replay.outside = None
    replay.controller.refresh()
    assert replay.controller._exit_due is True, (
        "a gap after the crossing must not withdraw the pending close"
    )


# --- the boundary that must NOT move ----------------------------------------

def test_an_untrusted_position_still_drops_the_arming():
    """position_trusted false means "this coordinate cannot be used", which is a
    different fact from "no coordinate this time". It must still clear, because a
    stale or contradictory reading could otherwise authorise a close."""
    replay = armed_and_inside()
    replay.trusted = False
    replay.outside = True
    asyncio.run(replay.poll())
    assert replay.controller._departure_armed is False
    assert replay.controller._exit_due is False
    assert not replay.closes()


def test_a_stale_robot_observation_still_drops_the_arming():
    replay = armed_and_inside()
    replay.fresh = False
    replay.outside = True
    asyncio.run(replay.poll())
    assert replay.controller._departure_armed is False
    assert replay.controller._exit_due is False
    assert not replay.closes()


@pytest.mark.parametrize("flag", ["trusted", "fresh"])
def test_untrusted_input_outranks_a_missing_sample(flag):
    """The untrusted branch is checked first: a bad position is unsafe even when
    the outside reading is also absent."""
    replay = armed_and_inside()
    setattr(replay, flag, False)
    replay.outside = None
    asyncio.run(replay.poll())
    assert replay.controller._departure_armed is False
    assert replay.controller._exit_due is False


def test_an_unarmed_controller_does_not_close_on_a_first_outside_sighting():
    """A reading we never armed for is not an observed departure."""
    replay = ExitReplay()
    replay.handover()
    asyncio.run(replay.start())
    replay.controller._departure_armed = False   # never saw the robot inside
    replay.outside = True
    asyncio.run(replay.poll(state="cleaning", cleaning=1, docked=False))
    assert not replay.closes()


def test_holding_mode_ignores_departures_entirely():
    """hold_open never closes on exit; the new branch must not leak into it."""
    replay = armed_and_inside()
    replay.controller.binding = dict(replay.controller.binding, task_door_mode="hold_open")
    replay.outside = None
    asyncio.run(replay.poll())
    replay.outside = True
    asyncio.run(replay.poll())
    assert not replay.closes()
