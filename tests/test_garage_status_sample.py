"""Execute actual coordinator methods while RPC/map boundaries are controlled."""

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest


PATH = Path(__file__).resolve().parents[1] / "custom_components/roborock_plus/coordinator.py"


class RpcError(Exception):
    pass


class UpdateFailed(Exception):
    def __init__(self, **_):
        super().__init__("update failed")


def methods(clock):
    """Compile production methods, not a test reimplementation of their logic."""
    tree = ast.parse(PATH.read_text(encoding="utf-8"))
    coordinator = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RoborockDataUpdateCoordinator")
    selected = [node for node in coordinator.body if isinstance(node, ast.AsyncFunctionDef) and node.name in {
        "_update_status", "_update_device_prop", "_async_update_data",
    }]
    helper = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_refresh_traits")
    wrapper = ast.ClassDef(name="Coordinator", bases=[], keywords=[], body=selected, decorator_list=[])
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), helper, wrapper], type_ignores=[])
    scope = {
        "dt_util": NS(utcnow=lambda: clock.now),
        "_LOGGER": NS(debug=lambda *_: None),
        "RoborockException": RpcError,
        "UpdateFailed": UpdateFailed,
        "DOMAIN": "roborock_plus",
        "IMAGE_CACHE_INTERVAL": timedelta(seconds=30),
        "V1_CLOUD_IN_CLEANING_INTERVAL": timedelta(seconds=30),
        "V1_CLOUD_NOT_CLEANING_INTERVAL": timedelta(seconds=60),
        "DeviceState": NS,
    }
    exec(compile(ast.fix_missing_locations(module), str(PATH), "exec"), scope)
    return scope["Coordinator"]


class Clock:
    now = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Harness:
    def __init__(self, *, full=False):
        self.clock = Clock()
        self.calls = []
        self.fail_status = False
        self.other_hook = None
        self.status = NS(state=NS(name="charging"), state_name="charging", in_cleaning=0, in_returning=0, refresh=self.status_rpc)
        self.other = NS(refresh=self.other_rpc)
        self.coordinator = methods(self.clock)()
        self.coordinator.properties_api = NS(
            status=self.status, consumables=self.other, clean_summary=None,
            dnd=None, dust_collection_mode=None, wash_towel_mode=None,
            smart_wash_params=None, sound_volume=None, child_lock=None,
            flow_led_status=None, valley_electricity_timer=None,
        )
        self.coordinator.garage_status_sample = None
        self.coordinator._device = NS(is_local_connected=True)
        self.coordinator._local_status_poll_interval = timedelta(seconds=1)
        self.coordinator._last_home_update_attempt = self.clock.now
        self.coordinator.last_update_state = "previous_state"
        self.coordinator._verify_api = self.verify
        self.coordinator._should_refresh_full_data = lambda _: full
        self.coordinator._should_suppress_update_failure = lambda: False
        self.coordinator._schedule_map_position_update = lambda _: None
        self.coordinator._async_update_map_if_due = self.map_rpc
        self.coordinator.data = "previous data"

    async def verify(self):
        pass

    async def status_rpc(self):
        self.calls.append("status")
        if self.fail_status:
            raise RpcError("status failed")

    async def other_rpc(self):
        self.calls.append("other")
        if self.other_hook is not None:
            await self.other_hook()

    async def map_rpc(self):
        self.calls.append("map")


def test_status_rpc_success_publishes_independent_scalar_snapshot():
    h = Harness()
    asyncio.run(h.coordinator._update_status())
    sample = h.coordinator.garage_status_sample
    assert sample == {"sampled_at": h.clock.now, "state": "charging", "in_cleaning": 0, "in_returning": 0}
    h.status.state.name = "cleaning"
    h.status.in_cleaning = 1
    assert sample["state"] == "charging"
    assert sample["in_cleaning"] == 0


def test_same_values_from_new_successful_rpc_have_new_sample_identity():
    h = Harness()
    async def exercise():
        await h.coordinator._update_status()
        previous = h.coordinator.garage_status_sample
        h.clock.advance(1)
        await h.coordinator._update_status()
        assert h.coordinator.garage_status_sample is not previous
        assert h.coordinator.garage_status_sample["sampled_at"] > previous["sampled_at"]
    asyncio.run(exercise())


def test_failed_status_rpc_cannot_refresh_old_snapshot_timestamp():
    h = Harness()
    async def exercise():
        await h.coordinator._update_status()
        previous = h.coordinator.garage_status_sample
        h.clock.advance(60)
        h.fail_status = True
        with pytest.raises(UpdateFailed):
            await h.coordinator._update_status()
        assert h.coordinator.garage_status_sample is previous
    asyncio.run(exercise())


def test_full_refresh_samples_status_before_other_traits_wait_or_fail():
    h = Harness(full=True)
    sampled_at = h.clock.now
    async def other():
        assert h.coordinator.garage_status_sample is not None
        assert h.coordinator.garage_status_sample["sampled_at"] == sampled_at
        h.clock.advance(20)
        raise RpcError("consumable failed")
    h.other_hook = other
    async def exercise():
        with pytest.raises(UpdateFailed):
            await h.coordinator._update_device_prop()
        assert h.coordinator.garage_status_sample["sampled_at"] == sampled_at
        assert h.calls == ["status", "other"]
    asyncio.run(exercise())


@pytest.mark.parametrize("full", [False, True])
def test_snapshot_exists_before_map_wait_and_keeps_actual_status_time(full):
    h = Harness(full=full)
    sampled_at = h.clock.now
    async def exercise():
        waiting, release = asyncio.Event(), asyncio.Event()
        async def map_wait():
            waiting.set()
            await release.wait()
        h.coordinator._async_update_map_if_due = map_wait
        update = asyncio.create_task(h.coordinator._async_update_data())
        try:
            await asyncio.wait_for(waiting.wait(), timeout=2)
            assert h.coordinator.garage_status_sample is not None
            assert h.coordinator.garage_status_sample["sampled_at"] == sampled_at
            h.clock.advance(60)
            h.status.state.name = "cleaning"
            h.status.in_cleaning = 1
        finally:
            release.set()
            await update
        sample = h.coordinator.garage_status_sample
        assert sample["sampled_at"] == sampled_at
        assert sample["state"] == "charging"
        assert sample["in_cleaning"] == 0
        assert h.coordinator._last_update_success_time == h.clock.now
    asyncio.run(exercise())


def test_full_refresh_does_not_retry_or_duplicate_failed_status():
    h = Harness(full=True)
    h.fail_status = True
    async def exercise():
        with pytest.raises(UpdateFailed):
            await h.coordinator._update_device_prop()
    asyncio.run(exercise())
    assert h.calls == ["status"]
    assert h.coordinator.garage_status_sample is None


def test_suppressed_coordinator_failure_does_not_create_a_new_status_sample():
    h = Harness()
    async def exercise():
        await h.coordinator._update_status()
        previous = h.coordinator.garage_status_sample
        h.coordinator._should_suppress_update_failure = lambda: True
        h.fail_status = True
        h.clock.advance(60)
        assert await h.coordinator._async_update_data() == "previous data"
        assert h.coordinator.garage_status_sample is previous
    asyncio.run(exercise())
