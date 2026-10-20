"""Fault recovery and return braking through the real controller, without devices."""

import asyncio

import pytest

from test_garage_after_exit import ExitReplay


async def outside_closed():
    replay = ExitReplay()
    replay.handover()
    await replay.start()
    await replay.depart()
    assert replay.controller.phase == "outside_closed"
    return replay


@pytest.mark.parametrize("missing", ["status", "position"])
def test_short_outside_observation_gap_recovers_same_task_and_normal_return(missing):
    async def exercise():
        r = await outside_closed()
        session_id = r.session.session_id
        if missing == "status":
            r.fresh = False
        else:
            r.outside = None
        await r.poll(.1)
        assert r.controller.reason is None
        assert r.controller.passage_allowed is not True
        assert len(r.closes()) == 1
        r.fresh, r.outside = True, True
        await r.poll(.1)
        await r.poll(.1, state="returning_home", returning=1)
        assert r.controller.reason is None
        assert r.session.session_id == session_id
        pause = r.events.index(("robot", "app_pause"))
        opening = r.events.index(("door", "open"), pause)
        resume = r.events.index(("robot", "app_resume"), opening)
        assert pause < opening < resume
        assert r.controller.phase == "returning"
    asyncio.run(exercise())


@pytest.mark.parametrize("missing", ["status", "position"])
def test_continuous_outside_observation_gap_times_out_then_only_brakes_return(missing):
    async def exercise():
        r = await outside_closed()
        if missing == "status":
            r.fresh = False
        else:
            r.outside = None
        await r.poll(.1)
        assert r.controller.reason is None
        await r.poll(r.controller.binding["operation_timeout_seconds"] + .1)
        assert r.controller.reason == "observation_timeout"
        assert r.controller.phase == "blocked"
        before = len(r.events)
        r.fresh, r.outside = True, True
        await r.poll(.1, state="returning_home", returning=1)
        assert r.state == "paused"
        assert r.events[before:].count(("robot", "app_pause")) == 1
        assert not any(event[0] == "door" for event in r.events[before:])
        assert ("robot", "app_resume") not in r.events[before:]
        assert r.controller.reason == "observation_timeout"
    asyncio.run(exercise())


@pytest.mark.parametrize("reason,manual", [
    ("task_end_unconfirmed", False),
    ("feedback_unavailable", False),
    ("position_untrusted", False),
    ("endpoint_conflict", False),
    ("manual_override", True),
])
def test_latched_fault_or_manual_hold_brakes_once_without_open_or_resume(reason, manual):
    async def exercise():
        r = await outside_closed()
        r.controller.fault(reason, manual=manual)
        phase = r.controller.phase
        before = len(r.events)
        await r.poll(state="returning_home", returning=1)
        assert r.state == "paused"
        assert r.events[before:].count(("robot", "app_pause")) == 1
        assert not any(event[0] == "door" for event in r.events[before:])
        assert ("robot", "app_resume") not in r.events[before:]
        assert (r.controller.reason, r.controller.phase) == (reason, phase)
        await r.poll()
        await r.poll()
        assert r.events[before:].count(("robot", "app_pause")) == 1
        assert (r.controller.reason, r.controller.phase) == (reason, phase)
    asyncio.run(exercise())


def test_failed_brake_on_latched_fault_waits_for_confirmation_and_does_not_retry_forever():
    async def exercise():
        r = await outside_closed()
        r.controller.start_timeout = 1
        r.controller.fault("feedback_unavailable")
        r.pause_works = False
        before = len(r.events)
        started = r.now
        await r.poll(.1, state="returning_home", returning=1)
        assert r.now - started >= 1
        assert r.events[before:].count(("robot", "app_pause")) == 1
        assert not any(event[0] == "door" for event in r.events[before:])
        assert ("robot", "app_resume") not in r.events[before:]
        assert r.controller.reason == "feedback_unavailable"
        await r.poll(.1)
        await r.poll(.1)
        assert r.events[before:].count(("robot", "app_pause")) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("revoked", ["user_pause", "cancel", "unknown_session"])
def test_revoked_return_authority_still_brakes_but_never_opens_or_resumes(revoked):
    async def exercise():
        r = await outside_closed()
        if revoked == "user_pause":
            r.controller.cancel_pending_for_user()
            r.state = "paused"
        elif revoked == "cancel":
            r.session.request_cancel()
        else:
            r.session = r.task_module.GarageTaskSession(5)
            r.controller.session = r.session
        before = len(r.events)
        await r.poll(state="returning_home", returning=1)
        assert r.state == "paused"
        assert ("robot", "app_pause") in r.events[before:]
        assert not any(event[0] == "door" for event in r.events[before:])
        assert ("robot", "app_resume") not in r.events[before:]
    asyncio.run(exercise())


@pytest.mark.parametrize("reason,manual", [
    ("task_end_unconfirmed", False),
    ("feedback_unavailable", False),
    ("manual_override", True),
])
def test_closed_stationary_clutch_release_preserves_exact_latched_state(reason, manual):
    async def exercise():
        r = await outside_closed()
        r.controller.fault(reason, manual=manual)
        previous = (r.controller.reason, r.controller.phase, r.controller.operation_id)
        before = list(r.events)
        r.controller.external_command("stop")
        assert (r.controller.reason, r.controller.phase, r.controller.operation_id) == previous
        assert r.events == before
        r.controller.external_command("stop", door_moving=True)
        assert r.controller.reason == "manual_override"
        assert r.controller.phase == "manual_hold"
    asyncio.run(exercise())


@pytest.mark.parametrize("missing", ["status", "position"])
def test_unknown_observation_cannot_start_a_new_departure_close(missing):
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        r.outside = False
        await r.poll(state="cleaning", cleaning=1, docked=False)
        r.outside = True if missing == "status" else None
        r.fresh = missing != "status"
        await r.poll()
        assert not r.closes()
        assert ("robot", "app_resume") not in r.events
    asyncio.run(exercise())


async def departing_with_stop_support(supported):
    """Prepare a real departure; stop only changes the virtual door input."""
    r = ExitReplay()
    r.handover()
    await r.start()
    r.outside = False
    await r.poll(state="cleaning", cleaning=1, docked=False)
    r.controller.supports_stop = lambda: supported
    original_move = r.move_door

    async def move(direction, operation):
        if direction == "stop":
            r.events.append(("door", "stop"))
            r.pending_door = None
        else:
            await original_move(direction, operation)

    r.controller.move_door = move
    r.events.clear()
    return r


@pytest.mark.parametrize("missing", ["status", "position"])
def test_closing_gap_stops_then_rechecks_before_reclosing(missing):
    async def exercise():
        r = await departing_with_stop_support(True)
        injected = False
        recovered = False

        def observations(replay):
            nonlocal injected, recovered
            if not injected and ("door", "close") in replay.events:
                injected = True
                if missing == "status":
                    replay.fresh = False
                else:
                    replay.outside = None
            elif injected and ("door", "stop") in replay.events and not recovered:
                recovered = True
                replay.fresh, replay.outside = True, True

        r.on_sleep = observations
        r.outside = True
        await r.poll()
        assert injected and recovered
        assert r.events.count(("door", "close")) == 2
        first = r.events.index(("door", "close"))
        stop = r.events.index(("door", "stop"))
        second = r.events.index(("door", "close"), first + 1)
        assert first < stop < second
        assert not any(event == ("robot", "app_pause") for event in r.events)
        assert r.controller.phase == "outside_closed"
        assert r.controller.reason is None
    asyncio.run(exercise())


def test_closing_gap_without_stop_reverses_open_and_does_not_reclose_on_recovery():
    async def exercise():
        r = await departing_with_stop_support(False)
        injected = False

        def observations(replay):
            nonlocal injected
            if not injected and ("door", "close") in replay.events:
                injected = True
                replay.outside = None
            elif injected and ("door", "open") in replay.events:
                replay.outside = True

        r.on_sleep = observations
        r.outside = True
        await r.poll()
        assert injected
        assert r.events.count(("door", "close")) == 1
        assert r.events.count(("door", "open")) == 1
        assert r.events.index(("door", "close")) < r.events.index(("door", "open"))
        assert r.open_contact == "on"
        assert r.controller.phase == "task_open"
        assert r.controller.reason is None
        await r.poll()
        await r.poll()
        assert r.events.count(("door", "close")) == 1
        assert not any(event == ("robot", "app_resume") for event in r.events)
    asyncio.run(exercise())


@pytest.mark.parametrize("supports_stop", [True, False])
def test_fresh_return_preempts_closing_gap_recovery(supports_stop):
    async def exercise():
        r = await departing_with_stop_support(supports_stop)
        injected = False
        returned = False

        def observations(replay):
            nonlocal injected, returned
            if not injected and ("door", "close") in replay.events:
                injected = True
                replay.fresh = False
            elif injected and not returned:
                returned = True
                replay.fresh, replay.outside = True, True
                replay.state, replay.in_returning = "returning_home", 1

        r.on_sleep = observations
        r.outside = True
        await r.poll()
        assert injected and returned
        assert r.events.count(("door", "close")) == 1
        pause = r.events.index(("robot", "app_pause"))
        resume = r.events.index(("robot", "app_resume"))
        assert pause < resume
        assert r.open_contact == "on"
        assert r.controller.phase == "returning"
        assert r.controller.reason is None
    asyncio.run(exercise())


def test_continuous_closing_gap_uses_one_deadline_and_never_retries_close():
    async def exercise():
        r = await departing_with_stop_support(True)

        def observations(replay):
            if ("door", "close") in replay.events:
                replay.fresh = False

        r.on_sleep = observations
        r.outside = True
        before = r.now
        await r.poll(.1)
        assert r.controller.reason == "observation_timeout"
        assert r.controller.phase == "blocked"
        assert r.events.count(("door", "close")) == 1
        assert r.events.count(("door", "stop")) == 1
        assert r.now - before <= r.controller.binding["operation_timeout_seconds"] + .5
        await r.poll()
        assert r.events.count(("door", "close")) == 1
    asyncio.run(exercise())


def test_user_pause_while_closing_gap_waits_revokes_reclose_generation():
    async def exercise():
        r = await departing_with_stop_support(True)
        cancelled = False

        def observations(replay):
            nonlocal cancelled
            if ("door", "stop") in replay.events and not cancelled:
                cancelled = True
                replay.controller.cancel_pending_for_user()
                replay.fresh, replay.outside, replay.state = True, True, "paused"
            elif ("door", "close") in replay.events and not cancelled:
                replay.fresh = False

        r.on_sleep = observations
        r.outside = True
        await r.poll()
        assert cancelled
        assert r.controller.phase == "manual_hold"
        assert r.events.count(("door", "close")) == 1
        assert ("robot", "app_resume") not in r.events
        await r.poll()
        assert r.events.count(("door", "close")) == 1
    asyncio.run(exercise())


def test_repeated_gap_recovery_cannot_extend_the_original_close_deadline():
    async def exercise():
        r = await departing_with_stop_support(True)
        before = r.now

        def observations(replay):
            if replay.now - before > 8:
                pytest.fail("restarting a close must not create an unbounded new deadline")
            closes = replay.events.count(("door", "close"))
            stops = replay.events.count(("door", "stop"))
            replay.fresh = stops >= closes

        r.on_sleep = observations
        r.outside = True
        await r.poll(.1)
        assert ("door", "stop") in r.events
        assert r.controller.reason in {"observation_timeout", "close_timeout"}
        assert r.controller.phase == "blocked"
        assert r.now - before <= r.controller.binding["operation_timeout_seconds"] + .5
        assert ("robot", "app_resume") not in r.events
    asyncio.run(exercise())


def test_replaced_task_during_close_recovery_cannot_adopt_a_fresh_return():
    async def exercise():
        r = await departing_with_stop_support(True)
        replaced = False

        def observations(replay):
            nonlocal replaced
            if ("door", "stop") in replay.events and not replaced:
                replaced = True
                replay.session.session_id = "replacement-task"
                replay.fresh, replay.outside = True, True
                replay.state, replay.in_returning = "returning_home", 1
            elif ("door", "close") in replay.events and not replaced:
                replay.fresh = False

        r.on_sleep = observations
        r.outside = True
        await r.poll()
        assert replaced
        assert r.events.count(("door", "close")) == 1
        assert ("robot", "app_resume") not in r.events
        assert ("door", "open") not in r.events
        assert ("robot", "app_pause") in r.events
        assert r.state == "paused"
        assert r.controller.phase == "blocked"
        assert r.controller.reason is not None
    asyncio.run(exercise())


@pytest.mark.parametrize("error_type", [OSError, TimeoutError])
def test_failed_door_stop_reverses_open_and_does_not_continue_blind_close(error_type):
    async def exercise():
        r = await departing_with_stop_support(True)
        move = r.controller.move_door

        async def fail_stop(direction, operation):
            if direction == "stop":
                r.events.append(("door", "stop-failed"))
                raise error_type("virtual stop RPC failed")
            await move(direction, operation)

        r.controller.move_door = fail_stop

        def observations(replay):
            if ("door", "open") in replay.events:
                replay.fresh, replay.outside = True, True
            elif ("door", "close") in replay.events:
                replay.fresh = False

        r.on_sleep = observations
        r.outside = True
        await r.poll()
        close = r.events.index(("door", "close"))
        failure = r.events.index(("door", "stop-failed"))
        opening = r.events.index(("door", "open"))
        assert close < failure < opening
        assert r.events.count(("door", "close")) == 1
        assert ("robot", "app_resume") not in r.events
        assert r.controller.phase == "task_open"
        assert r.controller.reason is None
        assert r.open_contact == "on"
        await r.poll()
        assert r.events.count(("door", "close")) == 1
    asyncio.run(exercise())


def test_recovered_gap_deadline_is_consumed_before_a_later_departure_gap():
    async def exercise():
        r = await outside_closed()
        r.fresh = False
        await r.poll(.1)
        assert r.controller.reason is None
        r.fresh = True
        await r.poll(.1, state="returning_home", returning=1)
        assert r.controller.phase == "returning"
        assert r.controller.reason is None
        r.outside = False
        await r.poll(.1, state="charging", cleaning=1, returning=0, docked=True)
        await r.poll(6)
        assert r.session.active
        await r.depart()
        assert len(r.closes()) == 2
        assert r.controller.phase == "outside_closed"
        r.fresh = False
        await r.poll(.1)
        assert r.controller.reason is None
        assert r.controller.phase == "outside_closed"
        await r.poll(r.controller.binding["operation_timeout_seconds"] + .1)
        assert r.controller.reason == "observation_timeout"
    asyncio.run(exercise())


def test_return_after_unpolled_gap_deadline_brakes_without_opening_or_resuming():
    async def exercise():
        r = await outside_closed()
        r.fresh = False
        await r.poll(.1)
        assert r.controller.reason is None
        r.advance(r.controller.binding["operation_timeout_seconds"] + .1)
        before = len(r.events)
        r.fresh = True
        await r.poll(.1, state="returning_home", returning=1)
        assert ("robot", "app_pause") in r.events[before:]
        assert r.state == "paused"
        assert not any(event[0] == "door" for event in r.events[before:])
        assert ("robot", "app_resume") not in r.events[before:]
        assert r.controller.reason == "observation_timeout"
        assert r.controller.phase == "blocked"
    asyncio.run(exercise())


def test_slow_door_stop_cannot_block_return_braking_or_release_its_new_owner():
    async def exercise():
        r = await departing_with_stop_support(True)
        stop_entered, release_stop = asyncio.Event(), asyncio.Event()
        pause_entered, release_pause = asyncio.Event(), asyncio.Event()
        move = r.controller.move_door
        send = r.controller.send_robot

        async def slow_move(direction, operation):
            if direction == "stop":
                r.events.append(("door", "stop-waiting"))
                stop_entered.set()
                await release_stop.wait()
                r.pending_door = None
                r.events.append(("door", "stop-completed"))
            else:
                await move(direction, operation)

        async def slow_pause(command):
            if command == "app_pause":
                r.events.append(("robot", "app_pause"))
                pause_entered.set()
                await release_pause.wait()
                r.advance(.1)
                r.state = "paused"
            else:
                await send(command)

        r.controller.move_door, r.controller.send_robot = slow_move, slow_pause
        r.on_sleep = lambda replay: setattr(replay, "fresh", False)
        r.outside = True
        close_task = asyncio.create_task(r.poll(.1))
        brake_task = None
        try:
            await asyncio.wait_for(stop_entered.wait(), .5)
            close_owner = r.controller._pending_operation
            r.on_sleep = None
            r.advance(.1)
            r.fresh, r.state, r.in_returning = True, "returning_home", 1
            brake_task = asyncio.create_task(r.controller.async_update())
            for _ in range(3):
                await asyncio.sleep(0)
            assert pause_entered.is_set(), "fresh return must brake before a slow stop RPC finishes"
            pause_owner = r.controller._pending_operation
            assert pause_owner is not None and pause_owner != close_owner
            assert r.controller._pending is True
            release_stop.set()
            await asyncio.wait_for(close_task, .5)
            assert r.controller._pending is True
            assert r.controller._pending_operation == pause_owner
            assert ("door", "open") not in r.events
            assert ("robot", "app_resume") not in r.events
            # The old operation's finally must not release the still-running
            # pause owner or let this tick start another command.
            await asyncio.wait_for(r.controller.async_update(), .5)
            assert r.events.count(("robot", "app_pause")) == 1
            release_pause.set()
            await asyncio.wait_for(brake_task, .5)
            assert r.state == "paused"
            assert r.controller.phase == "blocked"
            assert r.controller.reason is not None
            assert r.events.count(("door", "close")) == 1
            assert ("door", "open") not in r.events
            assert ("robot", "app_resume") not in r.events
        finally:
            release_stop.set()
            release_pause.set()
            pending = [close_task] + ([brake_task] if brake_task else [])
            await asyncio.gather(*pending, return_exceptions=True)
    asyncio.run(exercise())


def test_hung_gap_stop_is_bounded_by_the_remaining_close_deadline():
    async def exercise():
        r = await departing_with_stop_support(True)
        r.controller.binding = dict(r.controller.binding, operation_timeout_seconds=.4)
        move = r.controller.move_door
        never = asyncio.Event()

        async def hung_move(direction, operation):
            if direction == "stop":
                r.events.append(("door", "stop-waiting"))
                await never.wait()
            else:
                await move(direction, operation)

        r.controller.move_door = hung_move
        r.on_sleep = lambda replay: setattr(replay, "fresh", False)
        r.outside = True
        await asyncio.wait_for(r.poll(.1), .7)
        assert ("door", "stop-waiting") in r.events
        assert r.controller.reason in {"observation_timeout", "close_timeout"}
        assert r.controller.phase == "blocked"
        assert r.events.count(("door", "close")) == 1
        assert ("door", "open") not in r.events
        assert ("robot", "app_resume") not in r.events
    asyncio.run(exercise())


def test_pending_stop_takeover_preserves_a_new_external_manual_hold():
    async def exercise():
        r = await departing_with_stop_support(True)
        stop_entered, release_stop = asyncio.Event(), asyncio.Event()
        move = r.controller.move_door

        async def slow_move(direction, operation):
            if direction == "stop":
                r.events.append(("door", "stop-waiting"))
                stop_entered.set()
                await release_stop.wait()
                r.pending_door = None
            else:
                await move(direction, operation)

        r.controller.move_door = slow_move
        r.on_sleep = lambda replay: setattr(replay, "fresh", False)
        r.outside = True
        close_task = asyncio.create_task(r.poll(.1))
        try:
            await asyncio.wait_for(stop_entered.wait(), .5)
            r.controller.external_command("close", door_moving=True)
            assert (r.controller.reason, r.controller.phase) == ("manual_override", "manual_hold")
            r.on_sleep = None
            r.advance(.1)
            r.fresh, r.state, r.in_returning = True, "returning_home", 1
            await asyncio.wait_for(r.controller.async_update(), .5)
            assert r.state == "paused"
            assert r.events.count(("robot", "app_pause")) == 1
            assert (r.controller.reason, r.controller.phase) == ("manual_override", "manual_hold")
            release_stop.set()
            await asyncio.wait_for(close_task, .5)
            assert (r.controller.reason, r.controller.phase) == ("manual_override", "manual_hold")
            assert r.events.count(("robot", "app_pause")) == 1
            assert ("door", "open") not in r.events
            assert ("robot", "app_resume") not in r.events
        finally:
            release_stop.set()
            await asyncio.gather(close_task, return_exceptions=True)
    asyncio.run(exercise())
