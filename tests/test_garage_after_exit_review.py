"""Independent adversarial regressions for the after-exit controller."""

import asyncio

import pytest

from test_garage_after_exit import ExitReplay


def test_explicit_user_pause_outside_closed_revokes_automatic_return_resume():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        await r.depart()
        # The coordinator still exposes its last successful returning sample
        # when a user requests pause. Sending the user command must revoke
        # automatic ownership before the observer consumes that same sample.
        r.state, r.in_returning = "returning_home", 1
        r.advance(.1)
        r.controller.refresh()
        r.controller.cancel_pending_for_user()
        r.events.append(("user", "app_pause"))
        await r.controller.async_update()
        assert ("robot", "app_resume") not in r.events
        assert r.controller.phase == "manual_hold"
    asyncio.run(exercise())


def test_door_permission_loss_during_resume_cannot_report_return_success():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        await r.depart()
        original_send = r.send_robot

        async def send(command):
            await original_send(command)
            if command == "app_resume":
                r.open_contact = "unavailable"

        r.controller.send_robot = send
        await r.poll(state="returning_home", returning=1)
        assert r.controller.phase == "blocked"
        assert r.controller.reason == "feedback_unavailable"
    asyncio.run(exercise())


def test_closed_task_reports_loss_of_robot_observability():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        await r.depart()
        r.fresh = False
        await r.poll()
        assert r.controller.reason is not None
        assert r.controller.phase == "blocked"
    asyncio.run(exercise())


def test_delayed_completion_of_same_task_does_not_invalidate_final_return_pause():
    async def exercise():
        r = ExitReplay()
        r.handover()
        sent = await r.start()
        await r.depart()
        original_send = r.send_robot

        async def send(command):
            await original_send(command)
            if command == "app_pause":
                # A normal full-summary refresh catches up with the clean that
                # ended before its final return; it is not another task/cancel.
                r.summary = {
                    "records": [901, 900], "clean_count": 11,
                    "last_clean_record": {
                        "begin": int(sent), "end": int(r.utc), "complete": 1,
                        "finish_reason": "finished_cleaning", "error": 0,
                    },
                }

        r.controller.send_robot = send
        await r.poll(state="returning_home", cleaning=0, returning=1)
        assert ("robot", "app_resume") in r.events
        assert r.controller.reason is None
        assert r.session.active
    asyncio.run(exercise())


@pytest.mark.parametrize("bad", ["old_begin", "future_begin", "cancelled", "extra_task", "error"])
def test_delayed_completion_does_not_launder_another_or_cancelled_task(bad):
    async def exercise():
        r = ExitReplay()
        r.handover()
        sent = await r.start()
        await r.depart()
        original_send = r.send_robot

        async def send(command):
            await original_send(command)
            if command == "app_pause":
                detail = {"begin": int(sent), "end": int(r.utc), "complete": 1,
                          "finish_reason": "finished_cleaning", "error": 0}
                r.summary = {"records": [901, 900], "clean_count": 11, "last_clean_record": detail}
                if bad == "old_begin":
                    detail["begin"] = 500
                elif bad == "future_begin":
                    detail["begin"] = int(r.utc) + 10
                elif bad == "cancelled":
                    detail["complete"], detail["finish_reason"] = 0, "manual_interrupt"
                elif bad == "extra_task":
                    r.summary["records"], r.summary["clean_count"] = [902, 901, 900], 12
                elif bad == "error":
                    detail["error"] = 1

        r.controller.send_robot = send
        await r.poll(state="returning_home", cleaning=0, returning=1)
        assert ("robot", "app_resume") not in r.events
        assert r.controller.reason == "session_recovery_required"
    asyncio.run(exercise())


def test_explicit_user_resume_can_continue_a_valid_manually_paused_task():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        await r.depart()
        sid = r.session.session_id
        r.controller.cancel_pending_for_user()
        r.state = "paused"
        await r.poll()
        assert r.controller.phase == "manual_hold"
        assert ("robot", "app_resume") not in r.events
        await r.controller.async_execute("app_resume", lambda: r.send_robot("app_resume"), resume=True)
        await r.poll()
        assert r.controller.reason is None
        assert r.controller.phase == "returning"
        assert r.session.session_id == sid and r.session.active
        assert r.events.count(("robot", "app_resume")) == 1
    asyncio.run(exercise())


def test_door_stop_failure_cannot_skip_robot_pause_during_return_preemption():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        r.outside = False
        await r.poll(state="cleaning", cleaning=1, docked=False)
        original_move = r.move_door
        r.controller.supports_stop = lambda: True

        async def move(direction, operation):
            if direction == "stop":
                r.events.append(("door", "stop_failed"))
                raise RuntimeError("door stop failed")
            await original_move(direction, operation)

        r.controller.move_door = move
        triggered = False

        def returning(replay):
            nonlocal triggered
            if not triggered and replay.controller.phase == "closing":
                triggered = True
                replay.state, replay.in_returning = "returning_home", 1

        r.on_sleep = returning
        r.outside = True
        await r.poll()
        assert ("robot", "app_pause") in r.events
        assert ("door", "stop_failed") not in r.events
        assert r.controller.reason is None
        assert r.controller.phase == "returning"
        pause = r.events.index(("robot", "app_pause"))
        opening = r.events.index(("door", "open"), pause)
        resume = r.events.index(("robot", "app_resume"), opening)
        assert pause < opening < resume
    asyncio.run(exercise())


def test_required_open_reverse_failure_keeps_robot_paused_and_never_resumes():
    async def exercise():
        r = ExitReplay()
        r.handover()
        await r.start()
        r.outside = False
        await r.poll(state="cleaning", cleaning=1, docked=False)
        original_move = r.move_door

        async def move(direction, operation):
            if direction == "open" and r.controller.phase == "return_opening":
                r.events.append(("door", "reverse_open_failed"))
                raise RuntimeError("door reverse-open failed")
            await original_move(direction, operation)

        r.controller.move_door = move
        triggered = False

        def returning(replay):
            nonlocal triggered
            if not triggered and replay.controller.phase == "closing":
                triggered = True
                replay.state, replay.in_returning = "returning_home", 1

        r.on_sleep = returning
        r.outside = True
        await r.poll()
        assert r.events.index(("robot", "app_pause")) < r.events.index(("door", "reverse_open_failed"))
        assert r.state == "paused"
        assert ("robot", "app_resume") not in r.events
        assert r.controller.phase == "blocked"
        assert r.controller.reason == "command_failed"
    asyncio.run(exercise())
