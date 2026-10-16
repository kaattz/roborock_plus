"""Feedback recovery through the real HA runtime, using only memory devices."""

import asyncio
import importlib
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS

import pytest

from homeassistant.core import HomeAssistant

from ha_native_garage import MemorySessionStore, NativeRobotBoundary, binding


class NativeFeedbackRecovery(NativeRobotBoundary):
    def __init__(self, hass, mode):
        super().__init__(hass)
        self.runtime.shutdown()
        config = {
            **binding(), "task_door_mode": mode,
            "open_source": "timed", "close_source": "contact",
            "open_travel_seconds": 35, "travel_margin_seconds": 10,
            "operation_timeout_seconds": 90, "allow_estimated": True,
        }
        self.entry.options["garage_bindings"]["memory-robot"] = config
        module = importlib.import_module("custom_components.roborock_plus.garage_runtime")
        self.runtime = module.GarageRuntime(
            hass, self.coordinator, config, store=MemorySessionStore(),
            zone_provider=lambda: NS(min_x=0, max_x=10, min_y=0, max_y=10),
            clock=lambda: self.now, utc=lambda: self.utc,
        )
        self.coordinator.garage_runtime = self.runtime
        self.events = []

        async def sleep(seconds):
            self.observe(
                self.status.state.name, in_cleaning=self.status.in_cleaning,
                in_returning=self.status.in_returning, advance=seconds,
            )
            await asyncio.sleep(0)

        self.runtime.controller.sleep = sleep

        async def door_service(call):
            self.events.append((call.service, self.now))
            self.commands.append(call.service)
            if call.service != "stop_cover":
                # Immediate target echo must not shorten the full timed wait.
                self.set_door(call.service == "open_cover", call.context)

        for service in ("open_cover", "close_cover", "stop_cover"):
            hass.services.async_register("cover", service, door_service)

    async def feedback(self, available):
        self.observe("charging", advance=1)
        if available:
            self.set_door(False)
        else:
            self.hass.states.async_set(
                "cover.memory_garage", "unavailable",
                {"supported_features": 11, "current_position": 100},
            )
        await self.runtime.async_tick()
        await self.hass.async_block_till_done()

    async def start_robot(self):
        assert self.runtime.controller.passage_allowed is True
        self.events.append(("robot_start", self.now))
        self.commands.append("robot_start")
        self.observe("cleaning", in_cleaning=1, advance=0)
        return "accepted"


@pytest.mark.parametrize("mode", ["hold_open", "after_exit"])
def test_native_idle_feedback_recovery_requires_a_new_full_timed_open(mode):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, mode)
            try:
                await rig.initialize()
                await rig.feedback(False)
                assert rig.runtime.controller.passage_allowed is not True
                await rig.feedback(True)
                assert rig.commands == rig.events == []
                assert rig.runtime.controller.reason is None
                assert rig.runtime.controller.passage_allowed is not True
                assert await rig.runtime.async_send("app_start", rig.start_robot) == "accepted"
                await hass.async_block_till_done()
                assert [name for name, _ in rig.events] == ["open_cover", "robot_start"]
                assert rig.events[1][1] - rig.events[0][1] == 45
                assert rig.runtime.controller.state.open_confirmed is None
                assert rig.runtime.controller.reason is None
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["hold_open", "after_exit"])
def test_native_feedback_fault_stays_latched_and_reset_never_moves_devices(mode):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, mode)
            try:
                await rig.initialize()
                await rig.feedback(False)
                with pytest.raises(ValueError, match="feedback_unavailable"):
                    await rig.runtime.async_send("app_start", rig.start_robot)
                assert rig.runtime.controller.reason == "feedback_unavailable"
                await rig.feedback(True)
                assert rig.runtime.controller.reason == "feedback_unavailable"
                with pytest.raises(ValueError, match="feedback_unavailable"):
                    await rig.runtime.async_send("app_start", rig.start_robot)
                assert rig.events == rig.commands == []
                rig.runtime.reset()
                await hass.async_block_till_done()
                assert rig.runtime.controller.reason is None
                assert rig.events == rig.commands == []
                assert rig.runtime.controller.passage_allowed is not True
                assert await rig.runtime.async_send("app_start", rig.start_robot) == "accepted"
                await hass.async_block_till_done()
                assert [name for name, _ in rig.events] == ["open_cover", "robot_start"]
                assert rig.events[1][1] - rig.events[0][1] == 45
                assert rig.runtime.controller.reason is None
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["hold_open", "after_exit"])
def test_native_active_paused_reset_survives_ticks_before_explicit_resume(mode):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, mode)
            try:
                await rig.initialize()
                assert await rig.runtime.async_send("app_start", rig.start_robot) == "accepted"
                await hass.async_block_till_done()
                rig.observe("cleaning", in_cleaning=1)
                await rig.runtime.async_tick()
                assert rig.runtime.session.active and rig.runtime.session.started
                session_id = rig.runtime.session.session_id

                await rig.feedback(False)
                assert rig.runtime.controller.reason is not None
                await rig.feedback(True)
                rig.observe("paused")
                await rig.runtime.async_tick()
                await hass.async_block_till_done()
                assert rig.runtime.controller.reason is not None
                previous_events = list(rig.events)
                rig.runtime.reset()
                assert rig.runtime.controller.reason is None
                assert rig.runtime.controller.phase == "manual_hold"
                assert rig.events == previous_events

                for _ in range(3):
                    rig.observe("paused")
                    await rig.runtime.async_tick()
                    await hass.async_block_till_done()
                    assert rig.runtime.controller.reason is None
                    assert rig.runtime.controller.phase == "manual_hold"
                    assert rig.runtime.session.session_id == session_id
                    assert rig.runtime.session.active
                    assert rig.events == previous_events

                async def resume_robot():
                    assert rig.runtime.controller.passage_allowed is True
                    assert rig.runtime.session.session_id == session_id
                    rig.events.append(("app_resume", rig.now))
                    rig.commands.append("app_resume")
                    rig.observe("cleaning", in_cleaning=1)
                    return "resumed"

                assert await rig.runtime.async_send("app_resume", resume_robot) == "resumed"
                await hass.async_block_till_done()
                new_events = rig.events[len(previous_events):]
                assert [name for name, _ in new_events] == ["open_cover", "app_resume"]
                assert new_events[1][1] - new_events[0][1] == 45
                assert rig.runtime.session.session_id == session_id
                assert rig.runtime.session.active
                assert rig.runtime.controller.reason is None
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())
