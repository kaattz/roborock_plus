"""Closed-door clutch releases must not revoke future electric operation."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from test_garage_feedback_recovery import RecoveryReplay
from test_garage_runtime import Boundaries


@pytest.mark.parametrize("mode", ["hold_open", "after_exit"])
def test_closed_release_does_not_hold_or_shorten_the_next_full_open(mode):
    replay = RecoveryReplay(mode=mode)
    replay.handover()
    controller = replay.controller
    operation = controller.operation_id
    for _ in range(2):
        controller.external_command("stop")
        assert controller.reason is None
        assert controller.phase == "idle"
        assert controller.operation_id == operation
        assert controller.passage_allowed is not True
        assert not replay.door_calls and not replay.robot_calls
    asyncio.run(replay.request())
    assert [call[1] for call in replay.door_calls] == ["open"]
    assert replay.robot_calls[0] - replay.door_calls[0][0] == 45


@pytest.mark.parametrize("phase", ["idle", "outside_closed", "waiting_close_permission"])
def test_only_inactive_door_phases_allow_closed_release(phase):
    replay = RecoveryReplay(mode="after_exit")
    replay.handover()
    replay.controller.phase = phase
    replay.controller.external_command("stop")
    assert replay.controller.reason is None
    assert replay.controller.phase == phase


def test_closed_release_observes_only_door_and_does_not_advance_robot_session():
    replay = RecoveryReplay(mode="after_exit")
    replay.handover()
    controller = replay.controller
    previous = replay.session.to_dict()

    def unexpected_robot_observation():
        pytest.fail("clutch-release classification must not advance task or departure state")

    controller.robot_observation = unexpected_robot_observation
    controller.external_command("stop")
    assert controller.reason is None
    assert replay.session.to_dict() == previous
    assert not controller._departure_armed and not controller._exit_due


def test_measured_position_can_confirm_a_closed_release():
    replay = RecoveryReplay()
    replay.handover()
    replay.config.update(close_source="position", position_kind="measured")
    replay.controller.evidence = type(replay.controller.evidence)(replay.config)
    replay.controller.external_command("stop")
    assert replay.controller.reason is None
    assert replay.controller.state.closed_confirmed is True


def test_cached_closed_feedback_cannot_hide_a_new_contact_change():
    replay = RecoveryReplay()
    replay.handover()
    assert replay.controller.state.closed_confirmed is True
    replay.close_contact = "off"
    replay.controller.external_command("stop")
    assert replay.controller.reason == "manual_override"


def test_explicit_stop_does_not_convert_existing_manual_hold_to_idle():
    replay = RecoveryReplay()
    replay.handover()
    replay.controller.fault("manual_override", manual=True)
    operation = replay.controller.operation_id
    replay.controller.external_command("stop")
    assert replay.controller.reason == "manual_override"
    assert replay.controller.phase == "manual_hold"
    assert replay.controller.operation_id >= operation
    assert not replay.robot_calls and not replay.door_calls


@pytest.mark.parametrize("flag", ["_user_hold", "_pause_session", "_closing_for_exit", "_closed"])
def test_closed_stop_does_not_bypass_outstanding_holds_or_lifecycle_flags(flag):
    replay = RecoveryReplay()
    replay.handover()
    setattr(replay.controller, flag, True)
    replay.controller.external_command("stop")
    assert replay.controller.reason == "manual_override"
    assert replay.controller.phase == "manual_hold"


@pytest.mark.parametrize("phase", [
    "recovering", "opening", "closing", "awaiting_start", "pausing_return",
    "return_opening", "resuming_return", "returning", "task_open", "docked_intermission",
])
def test_stop_during_a_control_flow_still_holds_even_if_contact_has_not_released(phase):
    replay = RecoveryReplay()
    replay.handover()
    replay.controller.phase = phase
    replay.controller.external_command("stop")
    assert replay.controller.reason == "manual_override"
    assert replay.controller.phase == "manual_hold"
    assert replay.controller.passage_allowed is False


@pytest.mark.parametrize("invalid", [
    "open", "contact_unavailable", "unavailable", "restored", "conflict", "unsettled", "timed", "estimated_position", "pending",
])
def test_closed_stop_exemption_requires_current_confirmed_feedback(invalid):
    replay = RecoveryReplay()
    replay.handover()
    controller = replay.controller
    if invalid == "open":
        replay.close_contact = "off"
    elif invalid == "contact_unavailable":
        replay.close_contact = "unavailable"
    elif invalid == "unavailable":
        replay.available = False
    elif invalid == "restored":
        replay.restored = True
    elif invalid == "conflict":
        replay.config["open_source"] = "contact"
        replay.open_contact = "on"
    elif invalid == "unsettled":
        replay.config["contact_settle_seconds"] = 5
    elif invalid == "timed":
        replay.config["close_source"] = "timed"
    elif invalid == "estimated_position":
        replay.config["close_source"] = "position"
    elif invalid == "pending":
        controller._pending = True
    if invalid in {"conflict", "unsettled", "timed", "estimated_position"}:
        controller.evidence = type(controller.evidence)(replay.config)
        if invalid in {"timed", "estimated_position"}:
            controller.evidence.note_command("close", replay.now, 1)
            replay.advance(50)
            controller.refresh()
            assert controller.state.close.quality == "estimated"
            assert controller.state.close.reached is True
    controller.external_command("stop")
    assert controller.reason == "manual_override"
    assert controller.phase == "manual_hold"
    assert not replay.robot_calls and not replay.door_calls


@pytest.mark.parametrize("reason", ["manual_override", "feedback_unavailable", "open_timeout"])
def test_closed_release_never_clears_an_existing_hold_or_fault(reason):
    replay = RecoveryReplay()
    replay.handover()
    replay.controller.fault(reason, manual=reason == "manual_override")
    replay.controller.external_command("stop")
    assert replay.controller.reason is not None
    assert replay.controller.phase in {"manual_hold", "blocked"}
    with pytest.raises(ValueError):
        asyncio.run(replay.request())
    assert not replay.robot_calls and not replay.door_calls


def test_stop_during_open_cancels_wait_before_robot_can_start():
    replay = RecoveryReplay()
    replay.handover()
    replay.on_sleep = lambda: replay.controller.external_command("stop")
    with pytest.raises(ValueError, match="manual_override"):
        asyncio.run(replay.request())
    assert not replay.robot_calls
    assert replay.controller.phase == "manual_hold"


def closed_runtime():
    boundaries = Boundaries()
    runtime = boundaries.create()
    boundaries.cover.state = "closed"
    boundaries.states["binary_sensor.open"].state = "off"
    boundaries.states["binary_sensor.closed"].state = "on"
    runtime.controller.phase = "idle"
    runtime.controller.refresh()
    return boundaries, runtime


def service_event(service):
    return NS(context=NS(id="external", parent_id=None), data={
        "domain": "cover", "service": service,
        "service_data": {"entity_id": "cover.door"},
    })


def test_runtime_classifies_closed_stop_using_live_feedback():
    boundaries, runtime = closed_runtime()
    runtime._service_event(service_event("stop_cover"))
    assert runtime.controller.reason is None
    assert runtime.controller.phase == "idle"
    assert boundaries.events == []


@pytest.mark.parametrize("service", ["toggle", "set_cover_position", "open_cover", "close_cover"])
def test_other_external_commands_never_borrow_the_closed_stop_exemption(service):
    _, runtime = closed_runtime()
    runtime._service_event(service_event(service))
    assert runtime.controller.reason == "manual_override"


@pytest.mark.parametrize("state", ["opening", "closing"])
def test_runtime_still_observes_stop_of_a_moving_cover_at_closed_contact(state):
    boundaries, runtime = closed_runtime()
    boundaries.cover.state = state
    runtime._service_event(service_event("stop_cover"))
    assert runtime.controller.reason == "manual_override"


@pytest.mark.parametrize("target", ["cover.door", "cover.group", "all"])
@pytest.mark.parametrize("service", ["stop_cover", "toggle", "set_cover_position"])
def test_runtime_bound_group_and_all_targets_keep_stop_distinct_from_movement(target, service):
    boundaries, runtime = closed_runtime()
    boundaries.states["cover.group"] = NS(state="closed", attributes={"entity_id": ["cover.door", "cover.other"]})
    event = service_event(service)
    event.data["service_data"]["entity_id"] = target
    runtime._service_event(event)
    assert runtime.controller.reason == (None if service == "stop_cover" else "manual_override")


def test_stop_of_an_unrelated_cover_does_not_observe_or_change_this_binding():
    _, runtime = closed_runtime()

    def unexpected_observation(*args, **kwargs):
        pytest.fail("an unrelated cover service must exit before inspecting this door")

    runtime.evidence.observe = unexpected_observation
    event = service_event("stop_cover")
    event.data["service_data"]["entity_id"] = "cover.other"
    runtime._service_event(event)
    assert runtime.controller.reason is None


def test_own_context_is_ignored_before_external_movement_classification():
    _, runtime = closed_runtime()
    runtime._contexts.append("external")
    runtime._service_event(service_event("close_cover"))
    assert runtime.controller.reason is None

