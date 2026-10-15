"""Regression cases for initialization and external-door runtime boundaries."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from test_garage_runtime import Boundaries, MemoryStore, runtime_module


def sample(boundaries):
    boundaries.coord.garage_status_sample = {
        "sampled_at": datetime.fromtimestamp(boundaries.utc, timezone.utc),
        "state": boundaries.status.state.name,
        "in_cleaning": boundaries.status.in_cleaning,
        "in_returning": boundaries.status.in_returning,
    }


def advance(boundaries):
    boundaries.advance()
    sample(boundaries)


def test_restore_existing_payload_repairs_missing_initialization_marker():
    b = Boundaries()
    sample(b)
    seed = b.create()
    payload = seed.session.to_dict()
    payload.update(known_idle=True, reason=None)
    b.store.data["robot"] = deepcopy(payload)
    runtime = b.create()
    asyncio.run(runtime.async_restore())
    assert "robot" in b.entry.options.get("garage_initialized_duids", [])


def test_tick_cannot_initialize_or_overwrite_while_store_restore_is_pending():
    async def exercise():
        b = Boundaries()
        sample(b)
        seed = b.create()
        seed.session.begin(b.summary, b.utc, "app_start")
        active_payload = seed.session.to_dict()
        release = asyncio.Event()
        entered = asyncio.Event()

        class SlowStore(MemoryStore):
            def __init__(self):
                super().__init__()
                self.data["robot"] = deepcopy(active_payload)
                self.writes = []

            async def async_load(self):
                entered.set()
                await release.wait()

            async def async_save(self, duid, payload):
                self.writes.append(deepcopy(payload))
                await super().async_save(duid, payload)

        b.store = SlowStore()
        runtime = b.create()
        restore = asyncio.create_task(runtime.async_restore())
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            await runtime.async_tick()
            advance(b)
            await runtime.async_tick()
            assert b.store.writes == [], "unrestored ticks must not overwrite journal facts"
            assert runtime.session.known_idle is False
        finally:
            release.set()
            await restore
        assert runtime.session.active is True
        assert runtime.controller.reason == "session_recovery_required"
    asyncio.run(exercise())


def test_physical_closing_revokes_passage_before_open_contact_releases():
    b = Boundaries()
    sample(b)
    runtime = b.create()
    runtime.schedule = lambda: None
    runtime.session.begin(b.summary, b.utc, "app_start")
    runtime.controller.phase = "task_open"
    b.cover.state = "closing"
    b.cover.context = NS(id="physical-close", parent_id=None)
    event = NS(
        context=b.cover.context,
        data={"entity_id": "cover.door", "old_state": NS(state="open"), "new_state": b.cover},
    )
    runtime._state_event(event)
    assert b.states["binary_sensor.open"].state == "on"
    assert runtime.controller.phase == "manual_hold"
    assert runtime.controller.reason == "manual_override"
    assert runtime.controller.passage_allowed is False


def test_external_cover_group_close_resolves_bound_door_before_movement():
    b = Boundaries()
    sample(b)
    runtime = b.create()
    b.states["cover.group"] = NS(state="open", attributes={"entity_id": ["cover.door", "cover.other"]})
    runtime._service_event(NS(
        context=NS(id="group-close", parent_id=None),
        data={"domain": "cover", "service": "close_cover", "service_data": {"entity_id": "cover.group"}},
    ))
    assert runtime.controller.phase == "manual_hold"
    assert runtime.controller.reason == "manual_override"
    assert runtime.controller.passage_allowed is False


def test_late_coordinator_completion_cannot_refresh_old_status_sample():
    b = Boundaries()
    sample(b)
    runtime = b.create()
    b.now += 60
    b.utc += 60
    b.coord._last_update_success_time = datetime.fromtimestamp(b.utc, timezone.utc)
    assert runtime.robot_observation().fresh is False


def test_status_snapshot_values_are_used_instead_of_later_trait_mutation():
    b = Boundaries()
    sample(b)
    runtime = b.create()
    b.status.state.name = "cleaning"
    b.status.in_cleaning = 1
    observation = runtime.robot_observation()
    assert observation.state == "charging"
    assert observation.in_cleaning == 0


def status_poll(boundaries, runtime, sampled_utc, consumed_utc, *, state, cleaning):
    """Replay sampling followed by optional slow map work, without device I/O."""
    boundaries.utc = consumed_utc
    boundaries.now = consumed_utc - 1000
    boundaries.status.state.name = state
    boundaries.status.in_cleaning = cleaning
    boundaries.status.in_returning = 0
    boundaries.coord.garage_status_sample = {
        "sampled_at": datetime.fromtimestamp(sampled_utc, timezone.utc),
        "state": state, "in_cleaning": cleaning, "in_returning": 0,
    }
    boundaries.coord._last_update_success_time = datetime.fromtimestamp(consumed_utc, timezone.utc)
    runtime.controller.refresh()


def delayed_first_cleaning_runtime():
    b = Boundaries()
    b.coord.update_interval = timedelta(seconds=60)
    runtime = b.create()
    runtime.session.begin(b.summary, 1000, "app_start")
    status_poll(b, runtime, 1010, 1070, state="cleaning", cleaning=1)
    assert runtime.controller.robot.fresh is True
    return b, runtime


def test_delayed_status_consumption_does_not_accept_record_started_after_first_clean_sample():
    b, runtime = delayed_first_cleaning_runtime()
    b.summary.records = [2, 1]
    b.summary.clean_count = 2
    b.summary.last_clean_record = NS(begin=1040, end=1060, complete=1)
    status_poll(b, runtime, 1071, 1071, state="charging", cleaning=0)
    status_poll(b, runtime, 1073, 1073, state="charging", cleaning=0)
    assert runtime.session.active is True
    assert runtime.session.ended is False
    assert runtime.session.can_close is False
    assert runtime.session.to_dict()["first_clean_utc"] == 1010


def test_delayed_status_sample_allows_record_within_actual_start_window():
    b, runtime = delayed_first_cleaning_runtime()
    assert runtime.session.to_dict()["first_clean_utc"] == 1010
    b.summary.records = [2, 1]
    b.summary.clean_count = 2
    b.summary.last_clean_record = NS(begin=1005, end=1060, complete=1)
    status_poll(b, runtime, 1071, 1071, state="charging", cleaning=0)
    status_poll(b, runtime, 1073, 1073, state="charging", cleaning=0)
    assert runtime.session.ended is True
    assert runtime.session.can_close is True
    assert runtime.session.to_dict()["correlation_valid"] is True


@pytest.mark.parametrize("invalid", [999, 1021, float("nan"), float("inf"), True, "1001", 10**1000])
def test_invalid_first_clean_sample_time_invalidates_task_correlation(invalid):
    module = runtime_module()
    b = Boundaries()
    session = module.GarageTaskSession(1)
    session.begin(b.summary, 1000, "app_start")
    session.observe(module.RobotObservation(
        now=20, utc=1020, sample_utc=invalid, state="cleaning",
        in_cleaning=1, in_returning=0, sample_id=1, summary=b.summary,
        fresh=True, docked=False,
    ))
    assert session.active is True
    assert session.started is False
    assert session.to_dict()["correlation_valid"] is False
    assert session.can_close is False
