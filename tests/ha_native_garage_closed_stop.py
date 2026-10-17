"""Closed-door stop regression on the real HA bus, with memory devices only.

Explicit native runner; no account, integration setup, network or device access.
Uses the manifest's dependency versions rather than the historical 5.x pin.
"""

import asyncio
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

import pytest

from homeassistant.core import HomeAssistant

from ha_native_garage import NativeRobotBoundary
from ha_native_garage_feedback_recovery import NativeFeedbackRecovery


async def external_cover_service(hass, service, **extra):
    """Replay an external automation through HA's actual service/event path."""
    await hass.services.async_call(
        "cover", service, {"entity_id": "cover.memory_garage", **extra}, blocking=True,
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize("mode", ["hold_open", "after_exit"])
def test_native_closed_stop_preserves_idle_then_requires_full_new_open(mode):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, mode)
            try:
                await rig.initialize()
                controller = rig.runtime.controller
                assert controller.state.closed_confirmed is True
                operation = controller.operation_id
                session = rig.runtime.session.to_dict()
                await external_cover_service(hass, "stop_cover")
                assert controller.phase == "idle"
                assert controller.reason is None
                assert controller.operation_id == operation
                assert rig.runtime.session.to_dict() == session
                assert rig.commands == ["stop_cover"]
                assert await rig.runtime.async_send("app_start", rig.start_robot) == "accepted"
                await hass.async_block_till_done()
                assert [name for name, _ in rig.events] == ["stop_cover", "open_cover", "robot_start"]
                assert rig.events[2][1] - rig.events[1][1] == 45
                assert controller.state.open_confirmed is None
                assert controller.reason is None
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["hold_open", "after_exit"])
def test_native_stop_interrupts_real_open_wait_even_if_close_contact_stays_active(mode):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, mode)
            entered, release = asyncio.Event(), asyncio.Event()
            task = None
            try:
                await rig.initialize()

                async def stalled_open(call):
                    rig.commands.append(call.service)
                    rig.events.append((call.service, rig.now))
                    # Physical door has not left its closed switch yet.

                async def blocked_sleep(seconds):
                    entered.set()
                    await release.wait()
                    rig.observe("charging", advance=seconds)

                hass.services.async_register("cover", "open_cover", stalled_open)
                rig.runtime.controller.sleep = blocked_sleep
                task = asyncio.create_task(rig.runtime.async_send("app_start", rig.start_robot))
                await asyncio.wait_for(entered.wait(), 2)
                assert rig.runtime.controller.phase == "opening"
                assert rig.runtime.controller.state.closed_confirmed is True
                await external_cover_service(hass, "stop_cover")
                assert rig.runtime.controller.phase == "manual_hold"
                assert rig.runtime.controller.reason == "manual_override"
                release.set()
                with pytest.raises(ValueError, match="manual_override"):
                    await asyncio.wait_for(task, 2)
                assert rig.commands == ["open_cover", "stop_cover"]
            finally:
                release.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


def test_native_stop_interrupts_awaiting_start_after_pending_flag_is_released():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeRobotBoundary(hass)
            try:
                await rig.initialize()

                async def accepted_but_not_observed():
                    rig.commands.append("robot_start")
                    return "accepted"

                assert await rig.runtime.async_send("app_start", accepted_but_not_observed) == "accepted"
                assert rig.runtime.controller.phase == "awaiting_start"
                assert rig.runtime.controller._pending is False
                rig.set_door(False)
                await hass.async_block_till_done()
                rig.runtime.controller.refresh()
                assert rig.runtime.controller.state.closed_confirmed is True
                await external_cover_service(hass, "stop_cover")
                assert rig.runtime.controller.phase == "manual_hold"
                assert rig.runtime.controller.reason == "manual_override"
                before = list(rig.commands)
                rig.observe("cleaning", in_cleaning=1)
                await rig.runtime.async_tick()
                await hass.async_block_till_done()
                assert rig.runtime.controller.reason == "manual_override"
                assert rig.commands == before
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["hold_open", "after_exit"])
@pytest.mark.parametrize("service,extra", [
    ("toggle", {}), ("set_cover_position", {"position": 0}),
    ("set_cover_position", {"position": 100}),
])
def test_native_closed_movement_requests_are_not_mistaken_for_stop(mode, service, extra):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, mode)
            try:
                await rig.initialize()

                async def record_only(call):
                    rig.commands.append(call.service)

                hass.services.async_register("cover", service, record_only)
                await external_cover_service(hass, service, **extra)
                assert rig.runtime.controller.phase == "manual_hold"
                assert rig.runtime.controller.reason == "manual_override"
                assert rig.commands == [service]
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["hold_open", "after_exit"])
def test_native_closed_stop_never_releases_existing_manual_hold(mode):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, mode)
            try:
                await rig.initialize()
                rig.runtime.controller.fault("manual_override", manual=True)
                await external_cover_service(hass, "stop_cover")
                assert rig.runtime.controller.phase == "manual_hold"
                assert rig.runtime.controller.reason == "manual_override"
                with pytest.raises(ValueError, match="manual_override"):
                    await rig.runtime.async_send("app_start", rig.start_robot)
                assert rig.commands == ["stop_cover"]
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


@pytest.mark.parametrize("motion", ["opening", "closing"])
def test_native_cover_motion_reaches_stop_guard_even_before_closed_contact_releases(motion):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, "hold_open")
            try:
                await rig.initialize()
                received = []
                original = rig.runtime.controller.external_command

                def record_external(direction, *, door_moving=False):
                    received.append((direction, door_moving))
                    original(direction, door_moving=door_moving)

                rig.runtime.controller.external_command = record_external
                hass.states.async_set(
                    "cover.memory_garage", motion,
                    {"supported_features": 11, "current_position": 0},
                )
                # The physical closed contact still reads active at this instant.
                assert hass.states.get("binary_sensor.memory_closed").state == "on"
                await external_cover_service(hass, "stop_cover")
                assert ("stop", True) in received
                assert rig.runtime.controller.phase == "manual_hold"
                assert rig.runtime.controller.reason == "manual_override"
                assert rig.commands == ["stop_cover"]
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


def test_native_after_exit_closed_stop_keeps_active_outside_session():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, "after_exit")
            try:
                await rig.initialize()
                await rig.runtime.async_send("app_start", rig.start_robot)
                rig.observe("cleaning", in_cleaning=1, position=(5, 5))
                rig.coordinator.last_map_position_update = datetime.fromtimestamp(rig.utc, timezone.utc)
                await rig.runtime.async_tick()
                rig.observe("cleaning", in_cleaning=1, position=(20, 20))
                rig.coordinator.last_map_position_update = datetime.fromtimestamp(rig.utc, timezone.utc)
                await rig.runtime.async_tick()
                await hass.async_block_till_done()
                assert rig.runtime.controller.phase == "outside_closed"
                assert rig.runtime.controller.state.closed_confirmed is True
                assert rig.runtime.session.active
                session = rig.runtime.session.to_dict()
                before = list(rig.commands)
                await external_cover_service(hass, "stop_cover")
                assert rig.runtime.controller.phase == "outside_closed"
                assert rig.runtime.controller.reason is None
                assert rig.runtime.session.to_dict() == session
                assert rig.commands == [*before, "stop_cover"]
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())
