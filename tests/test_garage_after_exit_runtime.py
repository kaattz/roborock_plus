"""After-exit runtime inputs and internal commands stay on separate boundaries."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from test_garage_runtime import Boundaries


def moving_runtime(*, x=15, y=5, trusted=True):
    boundary = Boundaries(initialized=True)
    boundary.status.state.name = "cleaning"
    boundary.status.in_cleaning = 1
    boundary.advance()
    boundary.coord.last_map_position_update = datetime.fromtimestamp(boundary.utc, timezone.utc)
    boundary.coord.resolve_vacuum_position = lambda: NS(trusted=trusted, x=x, y=y, from_dock=False)
    runtime = boundary.create()
    return boundary, runtime


@pytest.mark.parametrize("x,y,outside", [
    (5, 5, False), (0, 0, False), (10, 10, False), (-1, 5, True), (11, 5, True), (5, 11, True),
])
def test_outside_danger_zone_uses_actual_trusted_rectangle(x, y, outside):
    boundary, runtime = moving_runtime(x=x, y=y)
    observation = runtime.robot_observation()
    assert observation.position_trusted is True
    assert observation.outside_danger_zone is outside
    assert observation.position_sample_id == boundary.coord.last_map_position_update.timestamp()


def test_status_poll_does_not_create_new_map_position_token():
    boundary, runtime = moving_runtime()
    first = runtime.robot_observation()
    boundary.advance()
    second = runtime.robot_observation()
    assert first.sample_id != second.sample_id
    assert first.position_sample_id == second.position_sample_id
    boundary.coord.last_map_position_update = datetime.fromtimestamp(boundary.utc, timezone.utc)
    assert runtime.robot_observation().position_sample_id != first.position_sample_id


@pytest.mark.parametrize("failure", ["untrusted", "missing_map_token", "future_map_token", "missing_zone",
    "invalid_zone", "nonfinite_zone", "missing_charger", "stale_status", "nonfinite_position"])
def test_uncertain_position_never_reports_safe_exit(failure):
    boundary, runtime = moving_runtime()
    if failure == "untrusted":
        boundary.coord.resolve_vacuum_position = lambda: NS(trusted=False, x=15, y=5)
    elif failure == "missing_map_token":
        boundary.coord.last_map_position_update = None
    elif failure == "future_map_token":
        boundary.coord.last_map_position_update = datetime.fromtimestamp(boundary.utc + 1, timezone.utc)
    elif failure == "missing_zone":
        runtime._zone_provider = lambda: None
    elif failure == "invalid_zone":
        runtime._zone_provider = lambda: NS(min_x=10, max_x=0, min_y=0, max_y=10)
    elif failure == "nonfinite_zone":
        runtime._zone_provider = lambda: NS(min_x=float("-inf"), max_x=float("inf"), min_y=0, max_y=10)
    elif failure == "missing_charger":
        boundary.coord.properties_api.map_content.map_data.charger = None
    elif failure == "stale_status":
        boundary.utc += 60
        boundary.now += 60
    elif failure == "nonfinite_position":
        boundary.coord.resolve_vacuum_position = lambda: NS(trusted=True, x=float("nan"), y=5)
    assert runtime.robot_observation().outside_danger_zone is None


def test_charging_uses_corroborated_dock_position_inside_danger_zone():
    boundary = Boundaries(initialized=True)
    boundary.coord.last_map_position_update = datetime.fromtimestamp(boundary.utc, timezone.utc)
    runtime = boundary.create()
    observation = runtime.robot_observation()
    assert observation.docked and observation.position_trusted
    assert observation.outside_danger_zone is False


@pytest.mark.parametrize("command", ["app_pause", "app_resume"])
def test_internal_robot_control_uses_raw_transport_and_refresh_without_user_override(command):
    boundary, runtime = moving_runtime()
    runtime._restored = True
    runtime._first_install = False
    trace = []
    response = {"result": "accepted"}
    async def transport(name):
        trace.append(("raw", name))
        return response
    async def refresh():
        trace.append(("refresh",))
    async def wrong_path(*args, **kwargs):
        pytest.fail("controller-owned pause/resume must not enter the user command path")
    boundary.coord.properties_api.command = NS(send=transport)
    boundary.coord.async_refresh = refresh
    runtime.async_send = wrong_path
    runtime.controller.cancel_pending_for_user = lambda: pytest.fail("internal pause is not user override")
    result = asyncio.run(runtime.async_control_command(command))
    assert result is response
    assert trace == [("raw", command), ("refresh",)]
    assert boundary.coord.last_resume_command == "app_resume"


@pytest.mark.parametrize("command", ["app_start", "app_charge", "app_stop", "get_status", "opaque_move"])
def test_internal_robot_control_rejects_every_other_command(command):
    boundary, runtime = moving_runtime()
    runtime._restored = True
    runtime._first_install = False
    calls = []
    async def transport(name):
        calls.append(name)
    boundary.coord.properties_api.command = NS(send=transport)
    with pytest.raises(Exception, match="unsupported_guarded_command"):
        asyncio.run(runtime.async_control_command(command))
    assert calls == []


def test_runtime_wires_internal_command_callback_into_controller():
    _, runtime = moving_runtime()
    assert runtime.controller.send_robot == runtime.async_control_command


def test_internal_robot_command_is_rejected_after_unload():
    boundary, runtime = moving_runtime()
    runtime._closed = True
    calls = []
    async def transport(name):
        calls.append(name)
    boundary.coord.properties_api.command = NS(send=transport)
    with pytest.raises(Exception, match="control_unloaded"):
        asyncio.run(runtime.async_control_command("app_resume"))
    assert calls == []


@pytest.mark.parametrize("phase", ["pausing_return", "return_opening", "resuming_return"])
@pytest.mark.parametrize("command", ["app_pause", "app_stop"])
def test_user_stop_pause_preempts_each_return_phase_before_transport(phase, command):
    boundary, runtime = moving_runtime()
    trace = []
    runtime.controller.phase = phase
    runtime.controller.cancel_pending_for_user = lambda: trace.append("cancel")
    runtime.schedule = lambda: None
    async def send():
        trace.append(command)
    asyncio.run(runtime.async_send(command, send))
    assert trace == ["cancel", command]


def test_own_return_opening_endpoint_release_is_not_reported_as_manual():
    boundary, runtime = moving_runtime()
    runtime.session.begin(boundary.summary, boundary.utc, "app_start")
    runtime.controller.phase = "return_opening"
    boundary.cover.state = "opening"
    boundary.states["binary_sensor.open"].state = "off"
    boundary.states["binary_sensor.closed"].state = "off"
    runtime.schedule = lambda: None
    runtime._contexts.append("our-open")
    runtime._state_event(NS(context=NS(id="our-open"), data={"entity_id": "cover.door", "new_state": boundary.cover}))
    assert runtime.controller.reason is None
    assert runtime.controller.phase == "return_opening"


@pytest.mark.parametrize("phase", ["pausing_return", "return_opening"])
def test_own_inflight_close_stays_controllable_while_return_preempts_it(phase):
    boundary, runtime = moving_runtime()
    runtime._contexts.append("our-old-close")
    boundary.cover.context = NS(id="our-old-close")
    boundary.cover.state = "closing"
    runtime.controller.phase = phase
    assert runtime.door_observation()["cover_available"] is True


@pytest.mark.parametrize("phase", ["pausing_return", "return_opening"])
def test_external_inflight_close_does_not_gain_internal_return_control(phase):
    boundary, runtime = moving_runtime()
    boundary.cover.context = NS(id="somebody-else")
    boundary.cover.state = "closing"
    runtime.controller.phase = phase
    assert runtime.door_observation()["cover_available"] is False
