"""after_exit uses real controllers with virtual robot, door and clock inputs."""

import asyncio
from dataclasses import replace

import pytest

from test_garage_workflow_replay import Replay


class ExitReplay(Replay):
    def __init__(self):
        self.outside = False
        self.return_state = "returning_home"
        self.pause_works = True
        self.resume_works = True
        self.on_sleep = None
        super().__init__()
        self.controller.binding = dict(self.controller.binding, task_door_mode="after_exit")
        self.controller.send_robot = self.send_robot

    def robot_observation(self):
        return replace(super().robot_observation(), outside_danger_zone=self.outside,
                       position_sample_id=self.sample_id)

    async def send_robot(self, command):
        self.events.append(("robot", command))
        self.advance(.1)
        if command == "app_pause" and self.pause_works:
            self.state = "paused"
        elif command == "app_resume" and self.resume_works:
            self.state = self.return_state
            self.in_returning = 1

    async def sleep(self, seconds):
        await super().sleep(seconds)
        if self.on_sleep:
            self.on_sleep(self)

    async def depart(self):
        self.outside = False
        await self.poll(state="cleaning", cleaning=1, returning=0, docked=False)
        self.outside = True
        await self.poll()


def test_every_departure_closes_without_pause_and_return_waits_for_open_then_resumes():
    async def exercise():
        r = ExitReplay()
        r.handover()
        sent = await r.start()
        await r.depart()
        sid = r.session.session_id
        assert r.controller.phase == "outside_closed"
        assert len(r.closes()) == 1
        assert ("robot", "app_pause") not in r.events
        await r.poll(state="going_to_wash_the_mop", returning=1)
        assert r.controller.phase == "returning"
        pause = r.events.index(("robot", "app_pause"))
        opening = r.events.index(("door", "open"), pause)
        resume = r.events.index(("robot", "app_resume"), opening)
        assert pause < opening < resume
        assert r.open_contact == "on"
        r.outside = False
        await r.poll(state="charging", cleaning=0, returning=0, docked=True)
        await r.poll(6)
        assert r.session.active and len(r.closes()) == 1
        await r.depart()
        assert len(r.closes()) == 2
        assert r.session.session_id == sid
        r.summary = {"records": [901, 900], "clean_count": 11,
                     "last_clean_record": {"begin": int(sent), "end": int(r.utc), "complete": 1}}
        await r.poll(state="returning_home", cleaning=0, returning=1)
        r.outside = False
        await r.poll(state="charging", cleaning=0, returning=0, docked=True)
        await r.poll(6)
        assert r.session.ended and len(r.closes()) == 3
        assert r.controller.phase == "idle"
        assert ("robot", "app_charge") not in r.events
    asyncio.run(exercise())


@pytest.mark.parametrize("failure,reason", [("pause", "pause_not_observed"), ("resume", "resume_not_observed")])
def test_return_requires_observed_pause_and_original_resume(failure, reason):
    async def exercise():
        r = ExitReplay()
        r.controller.start_timeout = 1
        r.handover()
        await r.start()
        await r.depart()
        r.pause_works = failure != "pause"
        r.resume_works = failure != "resume"
        await r.poll(state="returning_home", returning=1)
        assert r.controller.reason == reason
        assert r.controller.phase == "blocked"
        if failure == "pause":
            assert ("robot", "app_resume") not in r.events
            assert r.events.count(("door", "open")) == 1
        assert len(r.closes()) == 1
    asyncio.run(exercise())


def test_unknown_position_and_manual_pause_cannot_create_departure():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        await r.poll(state="cleaning", cleaning=1, docked=False)
        r.outside = None
        await r.poll()
        r.outside = True
        await r.poll()
        assert not r.closes()
        r.outside = False
        await r.poll(state="paused")
        r.outside = True
        await r.poll()
        assert not r.closes()
        assert not any(e == ("robot", "app_resume") for e in r.events)
    asyncio.run(exercise())


def test_return_preempts_departure_close_and_opens_only_after_pause():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        r.outside = False
        await r.poll(state="cleaning", cleaning=1, docked=False)
        changed = False
        def returning(replay):
            nonlocal changed
            if not changed and replay.controller.phase == "closing":
                changed = True
                replay.state, replay.in_returning = "returning_home", 1
        r.on_sleep = returning
        r.outside = True
        await r.poll()
        assert r.controller.phase == "returning"
        close = r.events.index(("door", "close"))
        pause = r.events.index(("robot", "app_pause"))
        opening = r.events.index(("door", "open"), close)
        assert close < pause < opening < r.events.index(("robot", "app_resume"))
    asyncio.run(exercise())


def test_robot_must_remain_paused_during_the_whole_opening_wait():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        await r.depart()
        step = 0
        def moved(replay):
            nonlocal step
            if replay.controller.phase == "return_opening":
                step += 1
                replay.state = "cleaning" if step == 1 else "paused"
        r.on_sleep = moved
        await r.poll(state="returning_home", returning=1)
        assert r.controller.reason == "pause_not_observed"
        assert ("robot", "app_resume") not in r.events
    asyncio.run(exercise())


def test_lost_robot_observations_outside_closed_are_reported_not_silently_ignored():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        await r.depart()
        r.fresh = False
        await r.poll()
        assert r.controller.reason == "position_untrusted"
        assert r.controller.phase == "blocked"
        assert len(r.closes()) == 1
    asyncio.run(exercise())


def test_status_refresh_with_the_same_map_sample_cannot_invent_a_crossing():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        observe = r.controller.robot_observation
        r.controller.robot_observation = lambda: replace(observe(), position_sample_id="unchanged-map")
        r.outside = False
        await r.poll(state="cleaning", cleaning=1, docked=False)
        r.outside = True
        await r.poll()
        assert not r.closes()
    asyncio.run(exercise())


def test_return_pause_does_not_wait_for_a_failing_optional_door_stop():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        r.outside = False
        await r.poll(state="cleaning", cleaning=1, docked=False)
        r.controller.supports_stop = lambda: True
        original_move = r.controller.move_door
        async def move(direction, operation):
            if direction == "stop":
                raise OSError("optional stop is unavailable")
            await original_move(direction, operation)
        r.controller.move_door = move
        def returning(replay):
            if replay.controller.phase == "closing":
                replay.state, replay.in_returning = "returning_home", 1
        r.on_sleep = returning
        r.outside = True
        await r.poll()
        assert r.controller.phase == "returning"
        assert ("robot", "app_pause") in r.events
        assert r.controller.reason is None
    asyncio.run(exercise())


def test_loss_of_outside_guard_stops_an_in_task_close():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        r.outside = False
        await r.poll(state="cleaning", cleaning=1, docked=False)
        r.controller.supports_stop = lambda: True
        original_move = r.controller.move_door
        async def move(direction, operation):
            if direction == "stop":
                r.events.append(("door", "stop"))
                r.pending_door = None
            else:
                await original_move(direction, operation)
        r.controller.move_door = move
        def returned_inside(replay):
            if replay.controller.phase == "closing":
                replay.outside = False
        r.on_sleep = returned_inside
        r.outside = True
        await r.poll()
        assert r.controller.reason == "task_end_unconfirmed"
        assert ("door", "stop") in r.events
        assert r.controller.phase == "blocked"
        assert ("robot", "app_resume") not in r.events
    asyncio.run(exercise())


@pytest.mark.parametrize("record", [
    {"begin": 1006, "end": 1010, "complete": 0},
    {"begin": 500, "end": 600, "complete": 1},
])
def test_return_cannot_adopt_an_invalid_record_already_present_before_pause(record):
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        await r.depart()
        r.summary = {"records": [901, 900], "clean_count": 11, "last_clean_record": record}
        await r.poll(state="returning_home", cleaning=0, returning=1)
        assert ("robot", "app_pause") in r.events
        assert r.state == "paused"
        assert r.controller.phase == "blocked"
        assert r.controller.reason == "session_recovery_required"
        assert ("robot", "app_resume") not in r.events
    asyncio.run(exercise())


@pytest.mark.parametrize("initial", ["missing", "malformed", "invalid_then_restored", "clock_break"])
def test_return_braking_is_independent_of_resume_authority(initial):
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        await r.depart()
        baseline = r.summary
        if initial == "missing":
            r.summary = None
        elif initial == "malformed":
            r.summary = {"records": [], "clean_count": "unknown"}
        elif initial == "invalid_then_restored":
            r.summary = {"records": [901, 900], "clean_count": 11,
                         "last_clean_record": {"begin": 1006, "end": 1010, "complete": 0}}
        else:
            r.utc_offset += 30
        original_send = r.send_robot
        async def send(command):
            await original_send(command)
            if command == "app_pause" and initial == "invalid_then_restored":
                r.summary = baseline
        r.controller.send_robot = send
        await r.poll(state="returning_home", cleaning=0, returning=1)
        pause = r.events.index(("robot", "app_pause"))
        assert r.events.index(("door", "open"), pause) > pause
        assert r.state == "paused"
        assert r.open_contact == "on"
        assert r.controller.phase == "blocked"
        assert ("robot", "app_resume") not in r.events
    asyncio.run(exercise())
