"""Real in-memory HA bus/services for the after-exit runtime boundary.

Explicitly invoked with the pinned Linux HA runtime. No real integrations,
credentials or physical devices are loaded.
"""

import asyncio
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS

from homeassistant.core import HomeAssistant

from ha_native_garage import NativeRobotBoundary


class NativeAfterExitBoundary(NativeRobotBoundary):
    def __init__(self, hass):
        super().__init__(hass)
        self.runtime.binding["task_door_mode"] = "after_exit"
        self.coordinator.last_map_position_update = datetime.fromtimestamp(self.utc, timezone.utc)

        async def send(command):
            self.commands.append(command)
            if command == "app_pause":
                self.observe("paused", in_cleaning=0, in_returning=0)
            elif command == "app_resume":
                self.observe("returning_home", in_cleaning=1, in_returning=1)
            else:
                raise AssertionError(f"unexpected internal command {command}")
            return {"result": "accepted"}

        async def refresh():
            return None

        self.coordinator.properties_api.command = NS(send=send)
        self.coordinator.async_refresh = refresh

    def position(self, state, *, in_cleaning=1, in_returning=0, point=(5, 5)):
        self.observe(state, in_cleaning=in_cleaning, in_returning=in_returning, position=point)
        self.coordinator.last_map_position_update = datetime.fromtimestamp(self.utc, timezone.utc)

    async def tick(self):
        await self.runtime.async_tick()
        await self.hass.async_block_till_done()

    async def begin(self):
        await self.initialize()
        sent = self.utc
        async def start():
            assert self.runtime.controller.passage_allowed is True
            self.commands.append("robot_start")
            self.position("cleaning")
            return "accepted"
        assert await self.runtime.async_send("app_start", start) == "accepted"
        await self.hass.async_block_till_done()
        return sent


def test_native_after_exit_closes_without_pause_and_each_return_pauses_before_open():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeAfterExitBoundary(hass)
            try:
                sent = await rig.begin()
                assert rig.commands == ["open_cover", "robot_start"]
                rig.position("cleaning", point=(20, 20))
                await rig.tick()
                assert rig.commands == ["open_cover", "robot_start", "close_cover"]
                rig.position("going_to_wash_the_mop", in_returning=1, point=(20, 20))
                await rig.tick()
                assert rig.commands[-3:] == ["app_pause", "open_cover", "app_resume"]
                assert rig.runtime.controller.reason is None
                assert rig.coordinator.last_resume_command == "app_resume"
                rig.position("washing_the_mop")
                await rig.tick()
                rig.position("charging")
                await rig.tick()
                assert rig.commands[-1] == "app_resume"
                assert rig.runtime.session.active
                rig.position("cleaning")
                await rig.tick()
                rig.position("cleaning", point=(20, 20))
                await rig.tick()
                assert rig.commands[-1] == "close_cover"
                assert rig.commands.count("app_pause") == 1
                rig.position("returning_home", in_returning=1, point=(20, 20))
                await rig.tick()
                assert rig.commands[-3:] == ["app_pause", "open_cover", "app_resume"]
                rig.summary.records, rig.summary.clean_count = [2, 1], 2
                rig.summary.last_clean_record = NS(begin=int(sent), end=int(rig.utc), complete=1, error=0)
                rig.position("charging", in_cleaning=0)
                await rig.tick()
                assert rig.commands[-1] == "app_resume"
                rig.position("charging", in_cleaning=0)
                await rig.tick()
                assert rig.runtime.session.ended
                assert rig.runtime.controller.phase == "idle"
                assert rig.commands[-1] == "close_cover"
                assert rig.commands.count("app_pause") == 2
                assert rig.commands.count("app_resume") == 2
                assert rig.runtime.controller.reason is None
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


def test_native_new_status_cannot_relabel_cached_map_as_new_exit_sample():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeAfterExitBoundary(hass)
            try:
                await rig.begin()
                map_token = rig.runtime.robot_observation().position_sample_id
                # Mutating a cached trait without its success token cannot
                # supply a second physical position observation.
                rig.x, rig.y = 20, 20
                for _ in range(3):
                    rig.observe("cleaning", in_cleaning=1)
                    await rig.tick()
                assert rig.runtime.robot_observation().position_sample_id == map_token
                assert rig.commands == ["open_cover", "robot_start"]
                assert rig.runtime.controller.reason is None
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())
