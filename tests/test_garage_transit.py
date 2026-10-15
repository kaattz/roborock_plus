"""Standalone HA movement has explicit transit facts, never fake clean records."""

import asyncio

import pytest

from test_garage_workflow_replay import Replay


async def send_transit(replay, command):
    async def send():
        replay.events.append(("robot", command))
    await replay.controller.async_execute(command, send, new_task=True)


def test_standalone_return_observes_return_and_settled_dock_then_closes():
    replay = Replay()
    replay.handover()
    async def exercise():
        await send_transit(replay, "app_charge")
        await replay.poll(state="returning_home", returning=1, docked=False)
        assert replay.session.started
        await replay.poll(state="charging", returning=0, docked=True)
        assert not replay.closes()
        await replay.poll(5)
        assert replay.session.ended and replay.session.known_idle
        assert replay.controller.reason is None
        assert replay.controller.phase == "idle"
        assert len(replay.closes()) == 1
    asyncio.run(exercise())


def test_return_when_already_docked_needs_post_send_fresh_settled_samples():
    replay = Replay()
    replay.handover()
    async def exercise():
        await send_transit(replay, "app_charge")
        await replay.controller.async_update()
        assert not replay.session.ended
        await replay.poll()
        assert not replay.session.ended
        await replay.poll(5)
        assert replay.session.ended and len(replay.closes()) == 1
    asyncio.run(exercise())


def test_goto_terminal_is_not_verified_idle_but_allows_explicit_return():
    replay = Replay()
    replay.handover()
    async def exercise():
        await send_transit(replay, "app_goto_target")
        await replay.poll(state="going_to_target", cleaning=0, returning=0, docked=False)
        assert replay.session.started
        await replay.poll(state="idle")
        assert replay.session.active
        await replay.poll(5)
        assert replay.session.ended and not replay.session.known_idle
        assert not replay.session.can_close and not replay.closes()
        previous = replay.session.session_id
        await send_transit(replay, "app_charge")
        assert replay.session.session_id != previous
        await replay.poll(state="returning_home", returning=1)
        await replay.poll(state="charging", returning=0, docked=True)
        await replay.poll(5)
        assert replay.session.known_idle and len(replay.closes()) == 1
    asyncio.run(exercise())


def test_goto_terminal_allows_explicit_new_clean_without_faking_clean_completion():
    replay = Replay()
    replay.handover()
    async def exercise():
        await send_transit(replay, "app_goto_target")
        await replay.poll(state="going_to_target", docked=False)
        await replay.poll(state="idle")
        await replay.poll(5)
        await replay.start()
        await replay.poll(state="cleaning", cleaning=1)
        assert replay.session.active and replay.session.task_kind == "clean"
        assert not replay.closes()
    asyncio.run(exercise())


@pytest.mark.parametrize("command", ["app_start", "app_charge", "app_goto_target"])
def test_external_unknown_task_cannot_be_overwritten_by_new_intent(command):
    replay = Replay()
    replay.handover()
    async def exercise():
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        assert not replay.session.known_idle
        with pytest.raises(Exception, match="session_recovery_required"):
            await send_transit(replay, command)
        assert not any(event[0] in ("robot", "door") for event in replay.events)
    asyncio.run(exercise())


def test_unobserved_goto_cannot_finish_from_idle_only():
    replay = Replay()
    replay.handover()
    async def exercise():
        await send_transit(replay, "app_goto_target")
        await replay.poll(state="idle", docked=False)
        await replay.poll(5)
        assert replay.session.active and not replay.session.started
        assert not replay.session.known_idle and not replay.closes()
    asyncio.run(exercise())


def test_transit_kind_round_trips_and_restart_never_replays():
    replay = Replay()
    replay.handover()
    async def exercise():
        await send_transit(replay, "app_goto_target")
        await replay.poll(state="going_to_target", docked=False)
        saved = replay.session.to_dict()
        restored = replay.task_module.GarageTaskSession(5)
        restored.restore(saved)
        assert restored.active and restored.task_kind == "goto"
        assert restored.reason == "session_recovery_required"
        assert not restored.can_close
        assert "command" not in saved and "deadline" not in saved
        replay.session = replay.controller.session = restored
        replay.controller.fault("session_recovery_required")
        before = list(replay.events)
        await replay.poll(state="going_to_target")
        assert replay.controller.reason == "session_recovery_required"
        assert replay.events == before
    asyncio.run(exercise())


def test_clean_session_return_keeps_clean_record_completion_contract():
    replay = Replay()
    replay.handover()
    async def exercise():
        await replay.start()
        await replay.poll(state="cleaning", cleaning=1, docked=False)
        identifier = replay.session.session_id
        async def send():
            replay.events.append(("robot", "app_charge"))
        await replay.controller.async_execute("app_charge", send)
        await replay.poll(state="returning_home", returning=1)
        await replay.poll(state="charging", cleaning=0, returning=0, docked=True)
        await replay.poll(5)
        assert replay.session.session_id == identifier
        assert replay.session.task_kind == "clean" and replay.session.active
        assert not replay.closes()
    asyncio.run(exercise())


def test_old_status_rpc_samples_do_not_count_toward_return_dock_settle():
    replay = Replay()
    replay.handover()
    async def exercise():
        await send_transit(replay, "app_charge")
        sent = replay.utc
        original = replay.robot_observation
        def delayed():
            observation = original()
            return replay.task_module.RobotObservation(**{
                **observation.__dict__, "sample_utc": sent - 1,
            })
        replay.controller.robot_observation = delayed
        await replay.poll()
        await replay.poll(5)
        assert replay.session.active and not replay.session.started
        replay.controller.robot_observation = original
        await replay.poll()
        assert replay.session.active, "one post-send poll cannot inherit pre-send dock stability"
        await replay.poll(5)
        assert replay.session.ended
    asyncio.run(exercise())


def test_failed_poll_restarts_goto_idle_stability():
    replay = Replay()
    replay.handover()
    async def exercise():
        await send_transit(replay, "app_goto_target")
        await replay.poll(state="going_to_target", docked=False)
        await replay.poll(state="idle")
        replay.fresh = False
        await replay.poll(4)
        replay.fresh = True
        await replay.poll()
        assert replay.session.active
        await replay.poll(5)
        assert replay.session.ended
    asyncio.run(exercise())


def test_external_task_after_goto_terminal_revokes_new_intent_permission():
    replay = Replay()
    replay.handover()
    async def exercise():
        await send_transit(replay, "app_goto_target")
        await replay.poll(state="going_to_target", docked=False)
        await replay.poll(state="idle")
        await replay.poll(5)
        await replay.poll(state="cleaning", cleaning=1)
        await replay.poll(state="idle", cleaning=0)
        with pytest.raises(Exception, match="session_recovery_required"):
            await send_transit(replay, "app_charge")
    asyncio.run(exercise())


def test_old_version_one_session_without_transit_fields_restores_as_clean():
    replay = Replay()
    replay.handover()
    payload = replay.session.to_dict()
    payload.pop("task_kind")
    payload.pop("first_transit_utc")
    restored = replay.task_module.GarageTaskSession(5)
    restored.restore(payload)
    assert restored.task_kind == "clean" and restored.known_idle
    assert not restored.can_close


@pytest.mark.parametrize('external_state,cleaning', [('cleaning', 1), ('going_to_target', 0), ('paused', 0)])
@pytest.mark.parametrize('restart', [False, True])
def test_external_task_irreversibly_revokes_old_goto_authority(external_state, cleaning, restart):
    replay = Replay()
    replay.handover()
    async def exercise():
        await send_transit(replay, 'app_goto_target')
        await replay.poll(state='going_to_target', docked=False)
        await replay.poll(state='idle')
        await replay.poll(5)
        assert replay.session.can_begin
        await replay.poll(state=external_state, cleaning=cleaning)
        await replay.poll(state='idle', cleaning=0, returning=0)
        assert replay.session.to_dict()['correlation_valid'] is False
        if restart:
            restored = replay.task_module.GarageTaskSession(5)
            restored.restore(replay.session.to_dict())
            replay.session = replay.controller.session = restored
            replay.controller.fault('session_recovery_required')
            await replay.poll()
        before = list(replay.events)
        with pytest.raises(Exception, match='session_recovery_required'):
            replay.controller.reset()
        assert not replay.session.can_begin and not replay.session.can_close
        with pytest.raises(Exception, match='session_recovery_required'):
            await send_transit(replay, 'app_charge')
        assert not any(event[0] in ('door', 'robot') for event in replay.events[len(before):])
    asyncio.run(exercise())


def test_external_task_revokes_verified_idle_across_quiet_dock_samples():
    replay = Replay()
    replay.handover()
    async def exercise():
        await replay.poll(state='cleaning', cleaning=1, docked=False)
        await replay.poll(state='charging', cleaning=0, docked=True)
        await replay.poll(5)
        assert replay.session.to_dict()['correlation_valid'] is False
        assert not replay.session.can_close
        with pytest.raises(Exception, match='session_recovery_required'):
            replay.controller.reset()
    asyncio.run(exercise())


def test_revoked_correlation_publishes_a_single_blocking_fault_without_actions():
    replay = Replay()
    replay.handover()
    async def exercise():
        before = list(replay.events)
        await replay.poll(state='cleaning', cleaning=1, docked=False)
        await replay.poll(state='idle', cleaning=0)
        await replay.poll(5)
        assert replay.controller.phase == 'blocked'
        assert replay.controller.reason == 'session_recovery_required'
        changes = replay.events[len(before):]
        assert changes.count(('issue', 'session_recovery_required')) == 1
        assert not any(event[0] in ('door', 'robot') for event in changes)
    asyncio.run(exercise())


def test_new_request_rechecks_external_task_before_background_tick_can_report_it():
    replay = Replay()
    replay.handover()
    replay.advance(1)
    replay.state, replay.in_cleaning, replay.docked = 'cleaning', 1, False
    before = list(replay.events)
    async def exercise():
        with pytest.raises(Exception, match='session_recovery_required'):
            await send_transit(replay, 'app_start')
        assert replay.session.to_dict()['correlation_valid'] is False
        assert not any(event[0] in ('door', 'robot') for event in replay.events[len(before):])
    asyncio.run(exercise())
