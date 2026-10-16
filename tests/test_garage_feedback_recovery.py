"""Replay feedback recovery through real modules; all I/O stays in memory."""

import asyncio

import pytest

from test_garage_workflow_replay import Replay, binding, production_modules


class RecoveryReplay(Replay):
    def __init__(self, source="timed", *, mode="hold_open"):
        super().__init__()
        self.available = True
        self.restored = False
        self.position = 0
        self.door_calls = []
        self.robot_calls = []
        self.on_sleep = None
        self.config = {
            **binding(), "task_door_mode": mode, "open_source": source,
            "position_kind": "estimated", "allow_estimated": True,
            "open_travel_seconds": 35, "travel_margin_seconds": 10,
            "operation_timeout_seconds": 90,
        }
        _, evidence, _ = production_modules()
        self.controller.binding = self.config
        self.controller.evidence = evidence.DoorEvidence(self.config)

    def door_observation(self):
        return {
            "cover_available": self.available, "restored": self.restored,
            "position": self.position, "open_contact": self.open_contact,
            "close_contact": self.close_contact,
        }

    async def move_door(self, direction, operation):
        self.door_calls.append((self.now, direction, operation))
        await super().move_door(direction, operation)
        # Target echo is immediate; it must never shorten an estimated wait.
        self.position = 100 if direction == "open" else 0

    async def sleep(self, seconds):
        await super().sleep(seconds)
        if self.on_sleep is not None:
            self.on_sleep()

    async def send(self):
        self.robot_calls.append(self.now)

    async def request(self):
        await self.controller.async_execute("app_segment_clean", self.send, new_task=True)

    def lose_then_recover(self):
        self.available = False
        self.controller.refresh()
        self.advance(1)
        self.available = True
        self.controller.refresh()


@pytest.mark.parametrize("source", ["timed", "position"])
@pytest.mark.parametrize("mode", ["hold_open", "after_exit"])
def test_idle_feedback_recovery_allows_a_new_request_after_full_fresh_travel(source, mode):
    replay = RecoveryReplay(source, mode=mode)
    replay.handover()
    replay.lose_then_recover()
    # Same sequence as 12:00 recovery -> 12:59 request. No automatic actions.
    assert replay.controller.passage_allowed is not True
    assert not replay.door_calls and not replay.robot_calls
    asyncio.run(replay.request())
    assert len(replay.door_calls) == len(replay.robot_calls) == 1
    assert replay.door_calls[0][1] == "open"
    assert replay.robot_calls[0] - replay.door_calls[0][0] == 45
    assert replay.controller.state.open_confirmed is None


@pytest.mark.parametrize("source", ["timed", "position"])
def test_explicit_reset_recovers_old_feedback_fault_without_any_device_action(source):
    replay = RecoveryReplay(source)
    replay.handover()
    replay.available = False
    with pytest.raises(replay.control_module.GarageControlError, match="feedback_unavailable"):
        asyncio.run(replay.request())
    replay.advance(1)
    replay.available = True
    replay.controller.refresh()
    assert replay.controller.reason == "feedback_unavailable"
    with pytest.raises(replay.control_module.GarageControlError, match="feedback_unavailable"):
        asyncio.run(replay.request())
    replay.controller.reset()
    assert replay.controller.reason is None
    assert replay.controller.passage_allowed is not True
    assert not replay.door_calls and not replay.robot_calls
    asyncio.run(replay.request())
    assert replay.robot_calls[0] - replay.door_calls[0][0] == 45


@pytest.mark.parametrize("reason", ["manual_override", "feedback_unavailable", "open_timeout"])
def test_feedback_recovery_does_not_clear_controller_fault_or_replay_a_request(reason):
    replay = RecoveryReplay()
    replay.handover()
    replay.controller.fault(reason, manual=reason == "manual_override")
    replay.lose_then_recover()
    asyncio.run(replay.controller.async_update())
    with pytest.raises(replay.control_module.GarageControlError, match=reason):
        asyncio.run(replay.request())
    assert replay.controller.reason == reason
    assert replay.controller.phase == ("manual_hold" if reason == "manual_override" else "blocked")
    assert not replay.door_calls and not replay.robot_calls


@pytest.mark.parametrize("source", ["timed", "position"])
def test_revoked_estimate_cancels_its_old_wait_even_if_feedback_recovers_before_next_poll(source):
    replay = RecoveryReplay(source)
    replay.handover()
    revoked = False

    def interrupt():
        nonlocal revoked
        if not revoked:
            revoked = True
            replay.available = False
            replay.controller.refresh()
            replay.available = True
            replay.controller.refresh()

    replay.on_sleep = interrupt
    started = replay.now
    with pytest.raises(replay.control_module.GarageControlError, match="operation_cancelled|feedback_unavailable"):
        asyncio.run(replay.request())
    assert replay.now - started < 1
    assert len(replay.door_calls) == 1
    assert not replay.robot_calls
    assert replay.controller.phase == "blocked"


@pytest.mark.parametrize("failure", ["cover", "restored", "position", "contact", "conflict", "clock", "session"])
def test_reset_keeps_rejecting_current_invalid_inputs(failure):
    replay = RecoveryReplay("contact" if failure in ("contact", "conflict") else "position")
    replay.handover()
    replay.controller.fault("feedback_unavailable")
    if failure == "cover":
        replay.available = False
    elif failure == "restored":
        replay.restored = True
    elif failure == "position":
        replay.position = None
    elif failure == "contact":
        replay.open_contact = "unavailable"
    elif failure == "conflict":
        replay.open_contact = replay.close_contact = "on"
    elif failure == "clock":
        replay.now -= 10
    else:
        replay.controller.session = replay.task_module.GarageTaskSession(5)
    with pytest.raises((replay.control_module.GarageControlError, replay.task_module.GarageTaskError)):
        replay.controller.reset()
    assert replay.controller.reason is not None
    assert not replay.door_calls and not replay.robot_calls


@pytest.mark.parametrize("source", ["timed", "position"])
@pytest.mark.parametrize("mode", ["hold_open", "after_exit"])
@pytest.mark.parametrize("restored_session", [False, True])
def test_active_paused_reset_waits_for_explicit_resume_across_background_ticks(source, mode, restored_session):
    async def exercise():
        replay = RecoveryReplay(source, mode=mode)
        replay.handover()
        await replay.request()
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        session_id = replay.session.session_id
        assert replay.session.started
        replay.available = False
        await replay.poll()
        assert replay.controller.reason == "feedback_unavailable"
        replay.available = True
        await replay.poll(state="paused", cleaning=0)
        if restored_session:
            restored = replay.task_module.GarageTaskSession(5)
            restored.restore(replay.session.to_dict())
            replay.session = replay.controller.session = restored
            replay.controller.fault("session_recovery_required")
        before = (list(replay.door_calls), list(replay.robot_calls))
        replay.controller.reset()
        for _ in range(3):
            await replay.poll()
        assert replay.controller.reason is None
        assert replay.controller.phase == "manual_hold"
        assert replay.controller.passage_allowed is not True
        assert replay.session.session_id == session_id
        assert (replay.door_calls, replay.robot_calls) == before
        await replay.controller.async_execute("app_resume", replay.send, resume=True)
        assert replay.robot_calls[-1] - replay.door_calls[-1][0] == 45
        assert replay.session.session_id == session_id
        await replay.poll(state="cleaning", cleaning=1)
        assert replay.controller.phase == "task_open"
    asyncio.run(exercise())
