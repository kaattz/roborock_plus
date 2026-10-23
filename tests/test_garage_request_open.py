""" ``roborock_plus.request_garage_open`` through the real controller.

The service exists so a household automation can ask the integration to open the
door instead of calling ``cover.open_cover`` behind its back: only the
integration knows how to wait out the full travel, keep the passage fact honest,
and avoid fighting a robot task. Two deliberately asymmetric decisions against
``async_request_close`` are pinned here:

* the family close-permission is not consulted (it only restricts closing);
* ``session.can_close`` is not consulted (it demands a settled dock, which is the
  opposite of "somebody wants to walk in right now").
"""

from __future__ import annotations

import asyncio

import pytest

from test_garage_feedback_recovery import RecoveryReplay
from test_garage_workflow_replay import Replay


def idle(replay: Replay) -> Replay:
    replay.handover()
    replay.controller.phase = "idle"
    replay.controller.refresh()
    assert replay.controller.reason is None
    return replay


# --- happy paths --------------------------------------------------------------

def test_it_opens_and_waits_out_the_whole_travel():
    replay = idle(RecoveryReplay(source="timed"))
    asyncio.run(replay.controller.async_request_open())
    assert [call[1] for call in replay.door_calls] == ["open"]
    # 35 s travel + 10 s margin from the fixture: the passage fact must not be
    # trusted before the door could physically have arrived.
    assert replay.controller.passage_allowed is True
    assert replay.controller.phase == "idle"
    assert replay.controller.reason is None


def test_it_persists_before_returning():
    replay = idle(RecoveryReplay(source="timed"))
    asyncio.run(replay.controller.async_request_open())
    assert replay.saved, "the new door fact must survive a restart"


def test_an_already_passable_door_gets_no_command_at_all():
    """The request is "let me through", not "make the motor move"."""
    # A contact source reports the endpoint without needing one of our commands,
    # so the door can genuinely already be passable here.
    replay = idle(RecoveryReplay(source="contact"))
    replay.open_contact = "on"
    replay.close_contact = "off"
    replay.controller.refresh()
    assert replay.controller.passage_allowed is True
    asyncio.run(replay.controller.async_request_open())
    assert replay.door_calls == []
    assert replay.robot_calls == []


def test_it_works_with_no_task_at_all():
    """`session.can_close` would refuse here; an open request must not need a task."""
    replay = idle(RecoveryReplay(source="timed"))
    assert replay.session.can_close is False or replay.session.known_idle
    assert not replay.session.active
    asyncio.run(replay.controller.async_request_open())
    assert [call[1] for call in replay.door_calls] == ["open"]


def test_the_family_close_permission_does_not_gate_opening():
    """Blocking automatic closing is not a request to be locked out."""
    replay = idle(RecoveryReplay(source="timed"))
    replay.controller.close_allowed = lambda: False
    asyncio.run(replay.controller.async_request_open())
    assert [call[1] for call in replay.door_calls] == ["open"]


# --- refusals, and none of them may touch the door ---------------------------

def test_a_feedback_fault_refuses_without_moving_the_door():
    replay = idle(RecoveryReplay(source="timed"))
    replay.available = False
    replay.controller.refresh()
    with pytest.raises(replay.control_module.GarageControlError, match="feedback_unavailable"):
        asyncio.run(replay.controller.async_request_open())
    assert replay.door_calls == []


def test_a_reported_closed_cover_without_observation_refuses():
    """A cover the runtime cannot observe must not be opened blind.

    ``_preflight`` only inspects endpoint evidence, which can still look healthy
    (a fresh, settled ``closed`` reading) while the cover itself is unavailable --
    for instance right after the entity went ``unknown``. Availability is a
    separate fact and needs its own guard, so this test deliberately keeps the
    evidence clean and only takes the cover away.
    """
    replay = idle(RecoveryReplay(source="timed"))
    clean_state = replay.controller.state
    assert clean_state.cover_available is True
    assert clean_state.open.reason not in (
        "endpoint_conflict", "feedback_unavailable", "restored_snapshot",
        "invalid_configuration", "invalid_clock",
    ), "the preflight must not be the thing that refuses this case"
    replay.controller.door_observation = lambda: {
        "cover_available": False, "restored": False, "position": None,
        "open_contact": None, "close_contact": None,
    }
    with pytest.raises(replay.control_module.GarageControlError, match="feedback_unavailable"):
        asyncio.run(replay.controller.async_request_open())
    assert replay.door_calls == []


def test_an_already_passable_door_does_not_persist_or_consume_an_operation():
    """The short circuit must return before acquiring anything."""
    replay = idle(RecoveryReplay(source="contact"))
    replay.open_contact = "on"
    replay.close_contact = "off"
    replay.controller.refresh()
    before = replay.controller.operation_id
    asyncio.run(replay.controller.async_request_open())
    assert replay.controller.operation_id == before
    assert replay.saved == [], "a no-op request must not rewrite the stored session"


def test_an_existing_hold_refuses_first():
    replay = RecoveryReplay()
    replay.handover()
    replay.controller.fault("feedback_unavailable")
    with pytest.raises(replay.control_module.GarageControlError, match="feedback_unavailable"):
        asyncio.run(replay.controller.async_request_open())
    assert replay.door_calls == []


def test_a_manual_hold_refuses_first():
    replay = RecoveryReplay()
    replay.handover()
    replay.controller.fault("manual_override", manual=True)
    with pytest.raises(replay.control_module.GarageControlError, match="manual_override"):
        asyncio.run(replay.controller.async_request_open())
    assert replay.door_calls == []


def test_an_in_flight_operation_refuses():
    replay = idle(RecoveryReplay(source="timed"))
    replay.controller._operations.add(42)
    with pytest.raises(replay.control_module.GarageControlError, match="operation_in_progress"):
        asyncio.run(replay.controller.async_request_open())
    assert replay.door_calls == []


def test_a_restored_snapshot_refuses():
    replay = idle(RecoveryReplay(source="timed"))
    replay.restored = True
    replay.controller.refresh()
    with pytest.raises(replay.control_module.GarageControlError, match="restored_snapshot"):
        asyncio.run(replay.controller.async_request_open())
    assert replay.door_calls == []


def test_a_stalled_door_times_out_instead_of_claiming_passage():
    replay = idle(RecoveryReplay(source="timed"))
    replay.controller.binding = {**replay.controller.binding, "operation_timeout_seconds": 5}
    # Never let the echo -> real travel happen: position stays at the closed end.
    replay.move_door = _stalled(replay)
    replay.controller.move_door = replay.move_door
    with pytest.raises(Exception):
        asyncio.run(replay.controller.async_request_open())
    assert replay.controller.passage_allowed is not True


def _stalled(replay):
    async def move_door(direction, operation):
        replay.door_calls.append((replay.now, direction, operation))
        # No travel: contacts keep reporting the closed endpoint.
    return move_door


# --- cancelling --------------------------------------------------------------

def test_cancelling_the_request_latches_operation_cancelled():
    replay = idle(RecoveryReplay(source="timed"))
    replay.on_sleep = lambda: replay.controller.fault("operation_cancelled", manual=False)
    with pytest.raises(Exception):
        asyncio.run(replay.controller.async_request_open())
    assert replay.controller.reason is not None
