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


def test_an_unknown_sample_alone_cannot_create_a_departure():
    """UNKNOWN IS NOT A DEPARTURE.

    The flag is armed by an explicit "inside" reading and fired by a later
    explicit "outside" one. A poll with no position at all must not substitute
    for either. This test used to assert the opposite for a different sequence
    (inside -> unknown -> outside), which pinned the 2026-10-25 defect: there the
    robot really did leave, and the unknown sample was one missed read that
    threw the arming away.
    """
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        await r.poll(state="cleaning", cleaning=1, docked=False)
        # Simulate a controller that never saw the robot inside this trip.
        r.controller._departure_armed = False
        r.outside = None
        await r.poll()
        assert not r.controller._departure_armed, "a missed sample cannot arm"
        r.outside = True
        await r.poll()
        assert not r.closes(), "an unarmed controller must not close on a first sighting outside"
    asyncio.run(exercise())


def test_a_missed_sample_does_not_lose_a_real_departure():
    """THE 2026-10-25 BUG: inside -> unknown -> outside must still close.

    Measured live: the door stayed open for 2m52s until it was shut by hand,
    because one unknown sample cleared the arming and every later "outside"
    reading found nothing to fire. The local server was answering map requests
    twice a second throughout.
    """
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        await r.poll(state="cleaning", cleaning=1, docked=False)
        r.outside = False
        await r.poll()
        assert r.controller._departure_armed, "an explicit inside reading arms the departure"
        r.outside = None
        await r.poll()
        assert r.controller._departure_armed, "a missed sample must not disarm"
        r.outside = True
        await r.poll()
        assert len(r.closes()) == 1, "the real departure must still close the door"
    asyncio.run(exercise())


def test_manual_pause_cannot_create_departure():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        await r.poll(state="cleaning", cleaning=1, docked=False)
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
        assert r.controller.reason is None
        await r.poll(r.controller.binding["operation_timeout_seconds"] + .1)
        assert r.controller.reason == "observation_timeout"
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


@pytest.mark.parametrize("silence", ["position", "status"])
def test_transient_silence_during_close_wait_does_not_latch_task_end(silence):
    """A missing sample cannot prove the robot came back.

    Replays 2026-10-18 09:02:24.94: the door was already closing, the robot was
    confirmed outside, and one unusable observation latched a permanent fault.
    Without STOP, abort the close by reopening; do not blindly finish closing.
    """
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        r.outside = False
        await r.poll(state="cleaning", cleaning=1, docked=False)
        steps = {"n": 0}

        def one_silent_sample(replay):
            if replay.controller.phase != "closing":
                return
            steps["n"] += 1
            if steps["n"] == 1:
                if silence == "position":
                    replay.outside = None
                else:
                    replay.fresh = False
            elif steps["n"] == 2:
                replay.outside = True
                replay.fresh = True

        r.on_sleep = one_silent_sample
        r.outside = True
        await r.poll()
        assert steps["n"] >= 1
        assert r.controller.reason is None
        assert r.controller.phase == "task_open"
        assert r.open_contact == "on"
        assert len(r.closes()) == 1
        assert "sleep" not in [event[0] for event in r.events]
    asyncio.run(exercise())


def test_silence_does_not_swallow_a_close_that_already_completed():
    """The physical close may finish while observations are missing.

    If the endpoint is reached, the already-commanded close succeeded; waiting
    for a liveness sample that never comes would turn a completed close into a
    timeout fault.
    """
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        r.outside = False
        await r.poll(state="cleaning", cleaning=1, docked=False)

        def silence_at_closed_endpoint(replay):
            if replay.controller.phase == "closing" and replay.close_contact == "on":
                replay.outside = None

        r.on_sleep = silence_at_closed_endpoint
        r.outside = True
        await r.poll()
        assert len(r.closes()) == 1
        assert r.close_contact == "on"
        assert r.controller.reason is None
        assert r.controller.phase == "outside_closed"
    asyncio.run(exercise())


def test_latched_fault_is_logged_once_with_its_reason(caplog):
    """`task_end_unconfirmed` used to change state without leaving any trace."""
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        r.outside = False
        await r.poll(state="cleaning", cleaning=1, docked=False)

        def returned_inside(replay):
            if replay.controller.phase == "closing":
                replay.outside = False

        r.on_sleep = returned_inside
        r.outside = True
        await r.poll()
        assert r.controller.reason == "task_end_unconfirmed"

    with caplog.at_level("WARNING"):
        asyncio.run(exercise())
    records = [record for record in caplog.records
               if record.name.endswith("garage_control")]
    assert len(records) == 1
    assert "task_end_unconfirmed" in records[0].getMessage()
    assert records[0].levelname == "WARNING"


@pytest.mark.parametrize("signal", ["docked", "cancel", "leaves_cleaning"])
def test_each_contradiction_term_stops_a_close_that_silence_would_not(signal):
    """Silence is tolerated; positive evidence against the departure is not.

    Covers the three contradiction terms that do not surface through
    `outside_danger_zone`, so a future edit cannot quietly drop one of them
    while the transient-silence test still passes.
    """
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        r.outside = False
        await r.poll(state="cleaning", cleaning=1, docked=False)

        def contradict(replay):
            if replay.controller.phase != "closing":
                return
            if signal == "docked":
                replay.state, replay.docked = "charging", True
            elif signal == "cancel":
                replay.session.request_cancel()
            else:
                replay.state = "paused"

        r.on_sleep = contradict
        r.outside = True
        await r.poll()
        assert r.controller.reason == "task_end_unconfirmed"
        assert r.controller.phase == "blocked"
        assert len(r.closes()) == 1
        assert ("robot", "app_resume") not in r.events

    asyncio.run(exercise())


def test_repeated_faults_with_the_same_reason_log_once(caplog):
    """A latched controller must not flood the log on every observation."""
    replay = Replay()
    replay.handover()
    with caplog.at_level("WARNING"):
        replay.controller.fault("task_end_unconfirmed")
        replay.controller.fault("task_end_unconfirmed")
        replay.controller.fault("task_end_unconfirmed")
    records = [record for record in caplog.records
               if record.name.endswith("garage_control")]
    assert len(records) == 1
    assert "task_end_unconfirmed" in records[0].getMessage()


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
        assert not any(event[0] == "door" for event in r.events[pause:])
        assert r.state == "paused"
        assert r.close_contact == "on"
        assert r.controller.phase == "blocked"
        assert ("robot", "app_resume") not in r.events
    asyncio.run(exercise())
