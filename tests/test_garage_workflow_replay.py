"""Replay robot/door inputs through the real task, evidence and controller."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1] / "custom_components/roborock_plus"


def production_modules():
    package = ModuleType("_garage_workflow_replay_tests")
    package.__path__ = [str(ROOT)]
    sys.modules.setdefault(package.__name__, package)
    return tuple(importlib.import_module(f"{package.__name__}.{name}")
                 for name in ("garage_task", "door_evidence", "garage_control"))


def binding():
    return {
        "task_door_mode": "hold_open", "open_source": "contact", "close_source": "contact",
        "open_contact": "binary_sensor.open", "close_contact": "binary_sensor.closed",
        "open_contact_state": "on", "close_contact_state": "on",
        "contact_settle_seconds": 0, "position_settle_seconds": 0,
        "open_position": 95, "close_position": 5, "open_travel_seconds": 1,
        "close_travel_seconds": 1, "travel_margin_seconds": 0,
        "operation_timeout_seconds": 3, "dock_settle_seconds": 5,
        "allow_estimated": False,
    }


class Replay:
    """External input recorder and deterministic door travel, never product logic."""

    def __init__(self):
        task, evidence, control = production_modules()
        self.task_module, self.control_module = task, control
        self.now = 0.0
        self.utc_offset = 1000.0
        self.sample_id = 0
        self.state = "charging"
        self.in_cleaning = 0
        self.in_returning = 0
        self.docked = True
        self.trusted = True
        self.fresh = True
        self.open_contact, self.close_contact = "off", "on"
        self.pending_door = None
        self.events = []
        self.saved = []
        self.summary = {
            "records": [900], "clean_count": 10,
            "last_clean_record": {"begin": 500, "end": 600, "complete": 1},
        }
        self.session = task.GarageTaskSession(5)
        self.controller = control.GarageController(
            binding(), evidence=evidence.DoorEvidence(binding()), session=self.session,
            move_door=self.move_door, door_observation=self.door_observation,
            robot_observation=self.robot_observation, close_allowed=lambda: True,
            persist=self.persist, notify=lambda reason: self.events.append(("issue", reason)),
            clock=lambda: self.now, sleep=self.sleep, start_timeout=60,
        )

    @property
    def utc(self):
        return self.utc_offset + self.now

    def robot_observation(self):
        return self.task_module.RobotObservation(
            now=self.now, utc=self.utc, state=self.state, in_cleaning=self.in_cleaning,
            in_returning=self.in_returning, summary=self.summary,
            position_trusted=self.trusted, docked=self.docked, fresh=self.fresh,
            sample_id=self.sample_id,
        )

    def door_observation(self):
        return {"cover_available": True, "open_contact": self.open_contact,
                "close_contact": self.close_contact}

    def advance(self, seconds):
        self.now += seconds
        self.sample_id += 1
        if self.pending_door is not None:
            direction, due = self.pending_door
            if self.now >= due:
                self.open_contact = "on" if direction == "open" else "off"
                self.close_contact = "on" if direction == "close" else "off"
                self.pending_door = None

    async def sleep(self, seconds):
        self.advance(seconds)
        await asyncio.sleep(0)

    async def move_door(self, direction, operation):
        self.events.append(("door", direction))
        self.open_contact = self.close_contact = "off"
        self.pending_door = (direction, self.now + 1)

    async def persist(self):
        self.saved.append(deepcopy(self.session.to_dict()))

    def handover(self):
        self.controller.refresh()
        self.advance(5)
        self.controller.refresh()
        self.session.initialize_idle()
        self.controller.reset()

    async def start(self):
        async def send():
            self.events.append(("robot", "app_start"))
        await self.controller.async_execute("app_start", send, new_task=True)
        return self.utc

    async def poll(self, seconds=1, *, state=None, cleaning=None, returning=None, docked=None):
        self.advance(seconds)
        for key, value in (("state", state), ("in_cleaning", cleaning),
                           ("in_returning", returning), ("docked", docked)):
            if value is not None:
                setattr(self, key, value)
        await self.controller.async_update()

    def closes(self):
        return [event for event in self.events if event == ("door", "close")]


def test_resume_rejects_empty_real_session_before_any_service():
    replay = Replay()
    replay.handover()

    async def exercise():
        async def send():
            replay.events.append(("robot", "app_resume"))
        with pytest.raises(replay.control_module.GarageControlError):
            await replay.controller.async_execute("app_resume", send, resume=True)

    asyncio.run(exercise())
    assert not any(event[0] in ("robot", "door") for event in replay.events)


def test_resume_charging_is_not_confirmation_that_original_task_continues():
    replay = Replay()
    replay.handover()

    async def exercise():
        await replay.start()
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        await replay.poll(state="paused", cleaning=1)

        async def send():
            replay.events.append(("robot", "app_resume"))
        await replay.controller.async_execute("app_resume", send, resume=True)
        await replay.poll(state="charging", cleaning=0, returning=0, docked=True)
        assert replay.controller.phase == "awaiting_start"
        await replay.poll(61)
        assert replay.controller.reason == "resume_not_observed"

    asyncio.run(exercise())
    assert not replay.closes()


def test_resume_return_state_without_active_task_flags_is_not_confirmation():
    replay = Replay()
    replay.handover()

    async def exercise():
        await replay.start()
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        await replay.poll(state="paused", cleaning=1)

        async def send():
            replay.events.append(("robot", "app_resume"))
        await replay.controller.async_execute("app_resume", send, resume=True)
        await replay.poll(state="returning_home", cleaning=0, returning=0, docked=False)
        assert replay.controller.phase == "awaiting_start"
        await replay.poll(61)
        assert replay.controller.reason == "resume_not_observed"

    asyncio.run(exercise())
    assert not replay.closes()


def test_explicit_resume_requires_new_poll_and_retains_original_session():
    replay = Replay()
    replay.handover()

    async def exercise():
        await replay.start()
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        identifier = replay.session.session_id
        await replay.poll(state="paused", cleaning=1)

        async def send():
            replay.events.append(("robot", "app_resume"))
        await replay.controller.async_execute("app_resume", send, resume=True)
        await replay.controller.async_update()
        assert replay.controller.phase == "awaiting_start"
        await replay.poll(state="cleaning", cleaning=1)
        assert replay.controller.phase == "task_open"
        assert replay.controller.reason is None
        assert replay.session.session_id == identifier
        assert replay.session.active

    asyncio.run(exercise())
    assert replay.events.count(("robot", "app_resume")) == 1
    assert not replay.closes()


def test_washing_preparation_longer_than_start_timeout_is_not_failed_start():
    replay = Replay()
    replay.handover()

    async def exercise():
        await replay.start()
        await replay.poll(state="washing_the_mop", cleaning=1, docked=True)
        await replay.poll(65)
        assert replay.controller.reason is None
        assert replay.session.active and not replay.session.started
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        await replay.poll(state="going_to_wash_the_mop", returning=1)
        await replay.poll(state="washing_the_mop", returning=0, docked=True)
        await replay.poll(65, state="charging", cleaning=1)
        assert replay.controller.reason is None
        assert replay.session.active

    asyncio.run(exercise())
    assert not replay.closes()


def test_reset_cannot_clear_a_current_endpoint_conflict():
    replay = Replay()
    replay.handover()
    replay.open_contact = replay.close_contact = "on"
    replay.controller.fault("endpoint_conflict")
    with pytest.raises(replay.control_module.GarageControlError, match="endpoint_conflict"):
        replay.controller.reset()
    assert replay.controller.reason == "endpoint_conflict"
    assert not any(event[0] == "door" for event in replay.events)


def test_reset_cannot_turn_lost_session_context_into_verified_idle():
    replay = Replay()
    replay.handover()
    replay.session = replay.task_module.GarageTaskSession(5)
    replay.controller.session = replay.session
    replay.controller.fault("session_recovery_required")
    replay.controller.refresh()
    replay.advance(5)
    replay.controller.refresh()
    with pytest.raises(replay.control_module.GarageControlError, match="session_recovery_required"):
        replay.controller.reset()
    assert replay.controller.reason == "session_recovery_required"
    assert not replay.session.known_idle


def test_reset_does_not_clear_persisted_clock_correlation_failure():
    replay = Replay()
    replay.handover()

    async def exercise():
        await replay.start()
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        replay.utc_offset += 1000
        replay.advance(1)
        replay.controller.refresh()
        replay.controller.fault("task_end_unconfirmed")
        assert replay.session.to_dict()["correlation_valid"] is False
        with pytest.raises(replay.control_module.GarageControlError):
            replay.controller.reset()
        assert replay.controller.reason is not None

    asyncio.run(exercise())


@pytest.mark.parametrize("state,cleaning", [("paused", 0), ("cleaning", 1)])
def test_explicit_reset_reconciles_valid_restored_task_without_replaying(state, cleaning):
    replay = Replay()
    replay.handover()

    async def exercise():
        await replay.start()
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        payload = replay.session.to_dict()
        restored = replay.task_module.GarageTaskSession(5)
        restored.restore(payload)
        replay.session = replay.controller.session = restored
        replay.controller.fault("session_recovery_required")
        await replay.poll(state=state, cleaning=cleaning, docked=False)
        before = list(replay.events)
        replay.controller.reset()
        assert replay.controller.reason is None
        assert replay.session.session_id == payload["session_id"]
        assert replay.session.active and not replay.session.can_close
        assert not any(event[0] in ("door", "robot") for event in replay.events[len(before):])
        async def resume():
            replay.events.append(("robot", "app_resume"))
        await replay.controller.async_execute("app_resume", resume, resume=True)
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        assert replay.controller.phase == "task_open"
        assert replay.events.count(("robot", "app_start")) == 1

    asyncio.run(exercise())


def test_restored_task_with_changed_record_baseline_cannot_be_reset_as_same_task():
    replay = Replay()
    replay.handover()

    async def exercise():
        await replay.start()
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        restored = replay.task_module.GarageTaskSession(5)
        restored.restore(replay.session.to_dict())
        replay.session = replay.controller.session = restored
        replay.controller.fault("session_recovery_required")
        replay.summary = {**replay.summary, "records": [901, 900], "clean_count": 11}
        await replay.poll(state="paused", cleaning=1, docked=False)
        with pytest.raises(replay.task_module.GarageTaskError, match="session_recovery_required"):
            replay.controller.reset()
        assert replay.controller.reason == "session_recovery_required"

    asyncio.run(exercise())


@pytest.mark.parametrize("direction", ["open", "close"])
def test_existing_endpoint_conflict_rejects_both_directions_before_service(direction):
    replay = Replay()
    replay.handover()
    replay.open_contact = replay.close_contact = "on"

    async def exercise():
        async def send():
            replay.events.append(("robot", "app_start"))
        with pytest.raises(replay.control_module.GarageControlError, match="endpoint_conflict"):
            if direction == "open":
                await replay.controller.async_execute("app_start", send, new_task=True)
            else:
                await replay.controller.async_request_close()

    asyncio.run(exercise())
    assert not any(event[0] in ("door", "robot") for event in replay.events)


def test_whole_task_intermission_resume_record_and_stable_dock_closes_once():
    replay = Replay()
    replay.handover()

    async def exercise():
        sent_utc = await replay.start()
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        assert replay.session.started
        await replay.poll(10, state="going_to_wash_the_mop", returning=1)
        await replay.poll(5, state="washing_the_mop", returning=0, docked=True)
        await replay.poll(65, state="charging", cleaning=1)
        assert replay.session.active and not replay.closes()
        await replay.poll(5, state="cleaning", cleaning=1, docked=False)
        assert replay.session.active and not replay.closes()
        replay.summary = {
            "records": [901, 900], "clean_count": 11,
            "last_clean_record": {"begin": int(sent_utc), "end": int(replay.utc),
                                  "complete": 1, "finish_reason": "finished_cleaning"},
        }
        await replay.poll(10, state="returning_home", cleaning=0, returning=1)
        await replay.poll(5, state="charging", returning=0, docked=True)
        assert not replay.closes()
        await replay.poll(5)
        assert replay.session.ended and replay.session.can_close
        assert replay.controller.phase == "idle"
        assert replay.controller.reason is None

    asyncio.run(exercise())
    assert replay.events.count(("robot", "app_start")) == 1
    assert replay.events.count(("door", "open")) == 1
    assert len(replay.closes()) == 1
    assert replay.saved[0]["active"] is True
    assert replay.saved[-1]["ended"] is True


def test_previous_late_record_does_not_close_the_current_task():
    replay = Replay()
    replay.handover()

    async def exercise():
        sent_utc = await replay.start()
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        replay.summary = {
            "records": [901, 900], "clean_count": 11,
            "last_clean_record": {"begin": int(sent_utc) - 1,
                                  "end": int(replay.utc), "complete": 1},
        }
        await replay.poll(10, state="charging", cleaning=0, returning=0, docked=True)
        await replay.poll(5)
        assert replay.session.active and not replay.session.ended
        assert replay.controller.phase == "docked_intermission"

    asyncio.run(exercise())
    assert not replay.closes()
