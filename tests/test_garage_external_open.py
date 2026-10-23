"""An external door command must be sized by the conflict it could cause, not by who sent it.

Measured twice on the live system: an external ``cover.open_cover`` while the
controller was idle with no task latched ``manual_hold``, and every later clean
command was refused until someone called ``reset_garage_control``:

    2026-10-22 16:08:38  Garage control held for manual handling:
                         reason=manual_override previous_phase=idle phase=manual_hold
    2026-10-23 12:48:26  vacuum.clean_area -> garage_control_error: manual_override

The hold exists so nothing steals the door from a person standing in it. At an
idle controller on a settled door there is nothing to steal from; holding there
only refuses the next schedule, which is policy, not safety.

The exemption must stay narrow: every one of the collision conditions below has
its own counter-example, and ``set_cover_position`` / ``toggle`` never qualify
because their direction depends on where the door already is.
"""

from __future__ import annotations

import asyncio

import pytest

from test_garage_feedback_recovery import RecoveryReplay


def idle(replay: RecoveryReplay) -> RecoveryReplay:
    """Hand over, then park the controller in the phase a quiet house sits in."""
    replay.handover()
    replay.controller.phase = "idle"
    replay.controller.refresh()
    assert replay.controller.reason is None
    return replay


# --- the conditions, each on its own -----------------------------------------

def test_stop_still_releases_the_clutch_without_a_hold():
    """The v0.7.8 behaviour must survive: a settled stop is a clutch release."""
    replay = idle(RecoveryReplay())
    replay.controller.external_command("stop")
    assert replay.controller.reason is None
    assert replay.controller.phase == "idle"
    assert not replay.door_calls and not replay.robot_calls


@pytest.mark.parametrize("direction", ["open", "close"])
def test_unconflicted_external_move_does_not_hold(direction):
    replay = idle(RecoveryReplay())
    before = (replay.controller.reason, replay.controller.phase, replay.controller.operation_id)
    replay.controller.external_command(direction)
    assert replay.controller.reason is None
    assert replay.controller.phase == "idle"
    assert replay.controller.operation_id == before[2], (
        "an observed command must not bump the operation generation"
    )
    assert not replay.door_calls and not replay.robot_calls


@pytest.mark.parametrize("direction", ["open", "close"])
def test_a_moving_door_is_still_a_conflict(direction):
    """The person is mid-push; do not pretend the command is harmless."""
    replay = idle(RecoveryReplay())
    replay.controller.external_command(direction, door_moving=True)
    assert replay.controller.reason == "manual_override"
    assert replay.controller.phase == "manual_hold"


@pytest.mark.parametrize(
    "direction", ["move", "position"],
)
def test_a_position_or_toggle_request_never_qualifies(direction):
    """Its effect depends on the current position, so there is no "no conflict" form."""
    replay = idle(RecoveryReplay())
    replay.controller.external_command(direction)
    assert replay.controller.reason == "manual_override"


@pytest.mark.parametrize("phase", [
    "recovering", "awaiting_start", "task_open", "returning",
    "pausing_return", "resuming_return", "docked_intermission",
    "closing", "blocked", "manual_hold",
])
def test_every_working_phase_still_holds(phase):
    replay = RecoveryReplay()
    replay.handover()
    replay.controller.phase = phase
    replay.controller.external_command("open")
    assert replay.controller.reason == "manual_override"
    assert replay.controller.phase == "manual_hold"


@pytest.mark.parametrize("phase", ["opening", "return_opening"])
def test_an_open_while_we_are_already_opening_is_ignored(phase):
    """Same direction, our own travel in flight: no conflict, and no state change.

    This predates the 2026-10-23 narrowing -- it is the original v0.7.x behaviour
    for an external ``open_cover`` that agrees with the opening we are performing.
    """
    replay = RecoveryReplay()
    replay.handover()
    replay.controller.phase = phase
    before = (replay.controller.reason, replay.controller.phase, replay.controller.operation_id)
    replay.controller.external_command("open")
    assert (replay.controller.reason, replay.controller.phase, replay.controller.operation_id) == before


def test_closing_while_we_are_opening_is_still_a_conflict():
    """The opposite direction during our travel is exactly the dangerous case."""
    replay = RecoveryReplay()
    replay.handover()
    replay.controller.phase = "opening"
    replay.controller.external_command("close", door_moving=True)
    assert replay.controller.reason == "manual_override"
    assert replay.controller.phase == "manual_hold"


@pytest.mark.parametrize("hold", ["_pending", "_pause_session", "_closing_for_exit"])
def test_outstanding_work_still_holds(hold):
    replay = idle(RecoveryReplay())
    setattr(replay.controller, hold, True)
    replay.controller.external_command("open")
    assert replay.controller.reason == "manual_override"


def test_an_in_flight_operation_still_holds():
    replay = idle(RecoveryReplay())
    replay.controller._operations.add(99)
    replay.controller.external_command("open")
    assert replay.controller.reason == "manual_override"


@pytest.mark.parametrize("reason", ["manual_override", "feedback_unavailable", "open_timeout"])
def test_an_existing_hold_is_never_cleared_by_an_external_move(reason):
    """Recovery of a sensor is not a reset, and neither is an external command.

    The reason may be re-stamped as ``manual_override`` (the pre-2026-10-23
    behaviour for a competing external move, kept here on purpose), but the
    controller must stay held -- never silently return to idle.
    """
    replay = RecoveryReplay()
    replay.handover()
    replay.controller.fault(reason, manual=reason == "manual_override")
    replay.controller.external_command("open")
    assert replay.controller.reason is not None
    assert replay.controller.phase in {"manual_hold", "blocked"}
    with pytest.raises(ValueError):
        asyncio.run(replay.request())
    assert not replay.robot_calls and not replay.door_calls


def test_unavailable_feedback_is_still_a_conflict():
    replay = idle(RecoveryReplay())
    replay.available = False
    replay.controller.refresh()
    replay.controller.external_command("open")
    assert replay.controller.reason == "manual_override"


def test_a_restored_snapshot_is_still_a_conflict():
    """``restored`` keeps cover_available True but poisons both endpoints.

    Without an explicit evidence check this slips past the availability guard and
    looks like a quiet house, which is exactly the state that must not be trusted.
    """
    replay = idle(RecoveryReplay())
    replay.restored = True
    replay.controller.refresh()
    assert replay.controller.state.cover_available is True
    assert replay.controller.state.open.reason == "restored_snapshot"
    replay.controller.external_command("open")
    assert replay.controller.reason == "manual_override"


def test_a_reason_with_an_idle_phase_is_still_a_conflict():
    """Defensive: today ``fault()`` always moves the phase off idle, but the
    exemption must not depend on that invariant holding forever."""
    replay = idle(RecoveryReplay())
    replay.controller.reason = "feedback_unavailable"
    assert replay.controller.phase == "idle"
    replay.controller.external_command("open")
    assert replay.controller.phase == "manual_hold"
    assert replay.controller.reason == "manual_override"


def test_an_unsettled_door_is_still_a_conflict():
    """A door still reporting 'opening'/'closing' has not settled; treat it as moving."""
    replay = idle(RecoveryReplay())
    replay.controller.state = replay.controller.state._replace(cover_available=False)
    replay.controller.external_command("open")
    assert replay.controller.reason == "manual_override"


# --- REQ-19: the exemption must not hand out passage -------------------------

def test_exemption_does_not_grant_passage():
    """No command of ours, so no timer, so no estimate: the schedule must open again."""
    replay = idle(RecoveryReplay(source="timed"))
    replay.controller.external_command("open")
    replay.controller.refresh()
    assert replay.controller.passage_allowed is not True
    assert replay.controller.state.open.reason == "command_required"


def test_the_next_clean_still_drives_the_door_once():
    """External open + scheduled clean = exactly two door commands, not one."""
    replay = idle(RecoveryReplay(source="timed"))
    replay.controller.external_command("open")
    assert replay.door_calls == []
    asyncio.run(replay.request())
    assert [call[1] for call in replay.door_calls] == ["open"]
    assert len(replay.door_calls) == 1
