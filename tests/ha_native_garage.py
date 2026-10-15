"""Explicit Linux/HA verification; never collected by the host's normal suite.

Run with the manifest's libraries and Home Assistant 2026.9.4 installed. Every
HA service below is registered inside a fresh in-memory HomeAssistant instance;
there are no credentials, real integrations, device connections, or network listeners.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import get_ident
from types import SimpleNamespace as NS

import pytest

from homeassistant.core import HomeAssistant, callback
# HA installs its voluptuous-compatible engine while importing the package.
# Import the public validation facade afterwards, just as HA integrations do.
import voluptuous as vol


PACKAGE = "custom_components.roborock_plus"


def test_native_imports_all_changed_production_modules():
    for name in (
        "config_flow", "coordinator", "garage_config", "door_evidence", "garage_guard",
        "garage_task", "garage_control", "garage_session_store", "garage_runtime",
        "garage_entities", "binary_sensor", "sensor", "vacuum", "button", "services",
    ):
        module = importlib.import_module(f"{PACKAGE}.{name}")
        assert module.__file__.startswith(str(Path(__file__).resolve().parents[1]))


def binding():
    return dict(
        enabled=True, cover_entity_id="cover.memory_garage", task_door_mode="hold_open",
        open_source="contact", close_source="contact", open_contact="binary_sensor.memory_open",
        close_contact="binary_sensor.memory_closed", open_contact_state="on", close_contact_state="on",
        open_travel_seconds=1, close_travel_seconds=1, travel_margin_seconds=0,
        contact_settle_seconds=0, position_settle_seconds=0, operation_timeout_seconds=3,
        dock_settle_seconds=1, allow_estimated=False, handover_confirmed=True,
    )


class MemoryEntries:
    """No account setup or reload; only the native flow's entry lookup boundary."""

    def __init__(self, entry):
        self.entry = entry

    @callback
    def async_entries(self, domain):
        return [self.entry] if domain == "roborock_plus" else []

    @callback
    def async_update_entry(self, entry, *, options):
        entry.options = deepcopy(options)
        return True


@pytest.mark.parametrize("mode", ["hold_open", "after_exit"])
@pytest.mark.parametrize("source", ["contact", "timed"])
def test_native_options_schema_and_saved_duid_binding(mode, source):
    async def exercise():
        flow_module = importlib.import_module(f"{PACKAGE}.config_flow")
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            previous = binding()
            previous.update(position_kind="measured", open_position=95, close_position=5)
            entry = NS(entry_id="isolated-entry", options={
                "garage_control_version": 1,
                "garage_bindings": {"memory-robot": previous},
                "garage_initialized_duids": ["memory-robot"],
            }, data={}, runtime_data=NS(
                v1=[NS(duid="memory-robot", device=NS(name="Memory robot"))],
            ))
            hass.config_entries = MemoryEntries(entry)
            hass.states.async_set("cover.memory_garage", "closed", {"supported_features": 3})

            class NativeOptions(flow_module.RoborockOptionsFlowHandler):
                @property
                def config_entry(self):
                    return entry

            flow = NativeOptions(entry)
            flow.hass = hass
            flow.context = {}
            flow.handler = "roborock_plus"
            flow.flow_id = "isolated-garage-options"
            normal = {
                "v1_local_status_poll_interval": 3, "v1_map_position_poll_interval": 5,
                "clean_command_watch_timeout": 90, "v1_stuck_detection_enabled": True,
                "v1_stuck_window": 120, "v1_stuck_radius": 200,
                "garage_guard_enabled": False, "garage_door_entity_id": "cover.memory_garage",
                "show_background": False, "show_rooms": True, "show_walls": True,
                "do_configure_garage": True,
            }
            page = await flow.async_step_init()
            assert isinstance(page["data_schema"], vol.Schema)
            normal = page["data_schema"](normal)
            page = await flow.async_step_drawables(normal)
            assert page["step_id"] == "garage_select"
            selected = page["data_schema"]({"duid": "memory-robot"})
            page = await flow.async_step_garage_select(selected)
            assert page["step_id"] == "garage"
            basics = {"enabled": True, "cover_entity_id": "cover.memory_garage",
                      "task_door_mode": mode, "open_source": source, "close_source": source}
            assert len(page["data_schema"].schema) == 6
            valid = page["data_schema"](basics)
            with pytest.raises(vol.Invalid):
                page["data_schema"]({**basics, "cover_entity_id": "invalid_entity_syntax"})
            rejected = await flow.async_step_garage({**valid, "cover_entity_id": "switch.wrong_domain"})
            assert rejected["errors"]["cover_entity_id"] == "invalid_entity_reference"
            result = await flow.async_step_garage(valid)
            visited = []
            values = {**binding(), **basics, "allow_estimated": source == "timed"}
            while result["type"] == "form":
                assert not result["errors"]
                step_id = result["step_id"]
                assert step_id not in visited
                visited.append(step_id)
                visible = {marker.schema for marker in result["data_schema"].schema}
                submitted = result["data_schema"]({key: value for key, value in values.items() if key in visible})
                result = await getattr(flow, f"async_step_{step_id}")(submitted)
            assert visited == (["garage_feedback", "garage_timing"] if source == "contact" else ["garage_timing"])
            assert result["type"] == "create_entry"
            assert result["data"]["garage_control_version"] == 1
            saved = result["data"]["garage_bindings"]["memory-robot"]
            assert saved == {**previous, **basics, "allow_estimated": source == "timed"}
            assert result["data"]["garage_initialized_duids"] == ["memory-robot"]
            assert "open_source" not in result["data"].get("drawables", {})

            # Re-enter through the native form, then switch sources. Hidden
            # calibrated hardware values must survive both saves unchanged.
            entry.options = deepcopy(result["data"])
            reopened = NativeOptions(entry)
            reopened.hass = hass
            reopened.context = {}
            reopened.handler = "roborock_plus"
            reopened.flow_id = "isolated-garage-options-reentry"
            page = await reopened.async_step_garage_select({"duid": "memory-robot"})
            assert page["data_schema"]({})["task_door_mode"] == mode
            other_source = "timed" if source == "contact" else "contact"
            change = page["data_schema"]({"open_source": other_source, "close_source": other_source})
            page = await reopened.async_step_garage(change)
            while page["type"] == "form":
                assert not page["errors"]
                submitted = page["data_schema"]({})
                if page["step_id"] == "garage_timing" and other_source == "timed":
                    submitted["allow_estimated"] = True
                page = await getattr(reopened, f"async_step_{page['step_id']}")(submitted)
            switched = page["data"]["garage_bindings"]["memory-robot"]
            assert switched["open_contact_state"] == "on"
            assert switched["open_travel_seconds"] == previous["open_travel_seconds"]
            assert switched["open_position"] == previous["open_position"]
            assert switched["position_kind"] == "measured"
            await hass.async_block_till_done()

    asyncio.run(exercise())


def test_native_service_registration_and_forbidden_force_argument():
    async def exercise():
        services = importlib.import_module(f"{PACKAGE}.services")
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            services.async_setup_services(hass)
            registered = hass.services.async_services()["roborock_plus"]
            for name in ("request_garage_close", "reset_garage_control"):
                assert hass.services.has_service("roborock_plus", name)
                schema = registered[name].schema
                schema({"entity_id": "vacuum.memory_robot"})
                with pytest.raises(vol.Invalid):
                    schema({"entity_id": "vacuum.memory_robot", "force": True})
            await hass.async_block_till_done()

    asyncio.run(exercise())


class MemorySessionStore:
    def __init__(self):
        self.data = {}

    async def async_load(self):
        pass

    def get(self, duid):
        return deepcopy(self.data.get(duid))

    async def async_save(self, duid, payload):
        self.data[duid] = deepcopy(payload)


def test_native_runtime_event_chain_opens_starts_finishes_and_closes(caplog):
    async def exercise():
        runtime_module = importlib.import_module(f"{PACKAGE}.garage_runtime")
        entity_module = importlib.import_module(f"{PACKAGE}.garage_entities")
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            moment = {"now": 0.0, "utc": 1000.0}
            commands = []
            issues = []
            entry = NS(entry_id="isolated-entry", options={
                "garage_control_version": 1, "garage_bindings": {"memory-robot": binding()},
            })
            hass.config_entries = MemoryEntries(entry)
            summary = NS(records=[1], clean_count=1, last_clean_record=NS(begin=500, end=600, complete=1))
            async def refresh_summary():
                return None
            status = NS(state=NS(name="charging"), in_cleaning=0, in_returning=0)
            coordinator = NS(
                duid="memory-robot", duid_slug="memory_robot", config_entry=entry,
                device_info={"identifiers": {("roborock_plus", "memory-robot")}},
                last_update_success=True, update_interval=timedelta(seconds=1),
                data=NS(status=status, clean_summary=summary),
                properties_api=NS(clean_summary=NS(refresh=refresh_summary),
                                  map_content=NS(map_data=NS(charger=NS(x=5, y=5)))),
                resolve_vacuum_position=lambda: NS(trusted=True, x=5, y=5, from_dock=True),
                async_add_listener=lambda _: (lambda: None), garage_setup_error=None,
            )
            def sample(advance=0):
                moment["now"] += advance
                moment["utc"] += advance
                coordinator.garage_status_sample = {
                    "sampled_at": datetime.fromtimestamp(moment["utc"], timezone.utc),
                    "state": status.state.name, "in_cleaning": status.in_cleaning,
                    "in_returning": status.in_returning,
                }
            sample()
            @callback
            def state_door(opened, context=None):
                hass.states.async_set("cover.memory_garage", "open" if opened else "closed",
                                      {"supported_features": 11, "current_position": 100 if opened else 0}, context=context)
                hass.states.async_set("binary_sensor.memory_open", "on" if opened else "off", context=context)
                hass.states.async_set("binary_sensor.memory_closed", "off" if opened else "on", context=context)
            state_door(False)
            async def door_service(call):
                commands.append(call.service)
                state_door(call.service == "open_cover", call.context)
            hass.services.async_register("cover", "open_cover", door_service)
            hass.services.async_register("cover", "close_cover", door_service)
            hass.bus.async_listen(runtime_module.EVENT_GARAGE_ISSUE, lambda event: issues.append(event.data))
            runtime = runtime_module.GarageRuntime(
                hass, coordinator, binding(), store=MemorySessionStore(),
                zone_provider=lambda: NS(min_x=0, max_x=10, min_y=0, max_y=10),
                clock=lambda: moment["now"], utc=lambda: moment["utc"],
            )
            coordinator.garage_runtime = runtime
            scheduling_threads = []
            original_schedule = runtime.schedule
            def record_schedule(*args):
                scheduling_threads.append(get_ident())
                original_schedule(*args)
            runtime.schedule = record_schedule
            await runtime.async_start()
            sample(1)
            await runtime.async_tick()
            assert runtime.session.known_idle
            assert entry.options["garage_initialized_duids"] == ["memory-robot"]
            assert commands == []
            async def start_robot():
                assert runtime.controller.passage_allowed is True
                commands.append("robot_start")
                status.state = NS(name="cleaning")
                status.in_cleaning = 1
                sample(1)
                return "accepted"
            assert await runtime.async_send("app_start", start_robot) == "accepted"
            await hass.async_block_till_done()
            assert runtime.controller.phase == "task_open"
            assert commands == ["open_cover", "robot_start"]
            status.state = NS(name="charging")
            status.in_cleaning = 0
            sample(1)
            summary.records = [2, 1]
            summary.clean_count = 2
            summary.last_clean_record = NS(begin=1001, end=int(moment["utc"]), complete=1, error=0)
            await runtime.async_tick()
            assert runtime.session.active
            sample(1)
            await runtime.async_tick()
            await hass.async_block_till_done()
            assert runtime.session.ended
            assert runtime.controller.phase == "idle"
            assert commands == ["open_cover", "robot_start", "close_cover"]
            entities = {entity._attr_translation_key: entity for entity in entity_module.build_garage_binary_entities([coordinator])}
            assert entities["garage_closed_confirmed"].is_on is True
            assert entities["garage_problem"].is_on is False
            runtime.controller.fault("feedback_unavailable")
            await hass.async_block_till_done()
            assert issues[-1]["reason"] == "feedback_unavailable"
            assert issues[-1]["phase"] == "blocked"
            assert issues[-1]["active"] is True
            runtime.shutdown()
            await hass.async_block_till_done()
            before = list(commands)
            hass.states.async_set("binary_sensor.memory_closed", "off")
            await hass.async_block_till_done()
            assert commands == before
            assert scheduling_threads
            assert set(scheduling_threads) == {get_ident()}, "HA event/timer callbacks must remain on the event-loop thread"

    asyncio.run(exercise())
    assert not [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize("field", ["cover_entity_id", "open_contact", "close_contact", "close_permission_entity"])
@pytest.mark.parametrize("action", ["rename", "remove"])
def test_native_registry_identity_tracks_binding_without_device_actions(field, action, caplog):
    async def exercise():
        from homeassistant.helpers import device_registry as dr, entity_registry as er
        module = importlib.import_module(f"{PACKAGE}.garage_runtime")
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            configured = {**binding(), "close_permission_entity": "binary_sensor.memory_permission"}
            entry = NS(entry_id="rename-entry", options={
                "garage_control_version": 1, "garage_bindings": {
                    "memory-robot": configured,
                    "draft-peer": {"enabled": False, field: configured[field], "handover_confirmed": True},
                },
                "unrelated_setting": "preserved",
            })
            hass.config_entries = MemoryEntries(entry)
            hass.data[dr.DATA_REGISTRY] = dr.DeviceRegistry(hass)
            await dr.async_load(hass, load_empty=True)
            await er.async_load(hass, load_empty=True)
            registry = er.async_get(hass)
            for key in ("cover_entity_id", "open_contact", "close_contact", "close_permission_entity"):
                entity_id = configured[key]
                domain, object_id = entity_id.split(".")
                registered = registry.async_get_or_create(domain, "memory_test", key, suggested_object_id=object_id)
                assert registered.entity_id == entity_id
                hass.states.async_set(entity_id, "closed" if domain == "cover" else "off", {"supported_features": 11})
            coordinator = NS(
                duid="memory-robot", config_entry=entry, last_update_success=True,
                update_interval=timedelta(seconds=1),
                garage_status_sample={"sampled_at": datetime.fromtimestamp(1000, timezone.utc), "state": "charging", "in_cleaning": 0, "in_returning": 0},
                data=NS(clean_summary=NS(records=[1], clean_count=1, last_clean_record=NS(begin=500, end=600, complete=1))),
                properties_api=NS(map_content=NS(map_data=NS(charger=NS(x=5, y=5)))),
                resolve_vacuum_position=lambda: NS(trusted=True, x=5, y=5),
                async_add_listener=lambda _: (lambda: None),
            )
            commands = []
            async def record_device_call(call):
                commands.append(call.service)
            for service in ("open_cover", "close_cover", "stop_cover"):
                hass.services.async_register("cover", service, record_device_call)
            session_module = importlib.import_module(f"{PACKAGE}.garage_task")
            previous_session = session_module.GarageTaskSession(1)
            previous_session.begin(coordinator.data.clean_summary, 1000)
            session_id = previous_session.session_id
            store = MemorySessionStore()
            store.data["memory-robot"] = previous_session.to_dict()
            runtime = module.GarageRuntime(hass, coordinator, configured, store=store,
                zone_provider=lambda: NS(min_x=0, max_x=10, min_y=0, max_y=10), clock=lambda: 0, utc=lambda: 1000)
            hass.data[module.REGISTRY_KEY] = {configured["cover_entity_id"]: runtime}
            await runtime.async_start()
            old = configured[field]
            new = old.split(".")[0] + ".renamed_" + old.split(".")[1]
            if action == "rename":
                registry.async_update_entity(old, new_entity_id=new)
            else:
                registry.async_remove(old)
                hass.states.async_remove(old)
                domain, object_id = old.split(".")
                replacement = registry.async_get_or_create(domain, "memory_test", "replacement_unique_id", suggested_object_id=object_id)
                assert replacement.entity_id == old
                hass.states.async_set(old, "open" if domain == "cover" else "on", {"supported_features": 11})
            await hass.async_block_till_done()
            try:
                if action == "rename":
                    assert runtime.binding[field] == new
                    assert runtime.controller.binding[field] == new
                    assert runtime.evidence._binding[field] == new
                    assert entry.options["garage_bindings"]["memory-robot"][field] == new
                    assert entry.options["garage_bindings"]["draft-peer"][field] == new
                    if field == "cover_entity_id":
                        assert old not in hass.data[module.REGISTRY_KEY]
                        assert hass.data[module.REGISTRY_KEY][new] is runtime
                else:
                    saved = entry.options["garage_bindings"]["memory-robot"]
                    assert saved[field] == old
                    assert saved["enabled"] is True
                    assert saved["handover_confirmed"] is False
                    assert entry.options["garage_bindings"]["draft-peer"]["handover_confirmed"] is False
                    assert runtime.binding["handover_confirmed"] is False
                    assert runtime.controller.reason == "feedback_unavailable"
                    assert runtime.door_observation()["cover_available"] is False
                    assert module.configured(coordinator) is True
                    with pytest.raises(module.GarageControlError, match="feedback_unavailable"):
                        runtime.controller.reset()
                    config = importlib.import_module(f"{PACKAGE}.garage_config")
                    with pytest.raises(config.GarageConfigError, match="handover_required"):
                        config.resolve_binding(entry.options, coordinator.duid)
                assert entry.options["unrelated_setting"] == "preserved"
                assert entry.options["garage_initialized_duids"] == ["memory-robot"]
                assert runtime.session.active is True
                assert runtime.session.session_id == session_id
                assert entry.options["garage_bindings"]["draft-peer"]["enabled"] is False
                assert runtime.controller.passage_allowed is not True
                assert commands == []
            finally:
                runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())
    assert not [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


class NativeRobotBoundary:
    """Reusable real-HA bus/services with an in-memory robot and full-travel door."""

    def __init__(self, hass):
        module = importlib.import_module(f"{PACKAGE}.garage_runtime")
        self.hass = hass
        self.now, self.utc = 0.0, 1000.0
        self.x, self.y = 5, 5
        self.commands = []
        self.summary = NS(records=[1], clean_count=1, last_clean_record=NS(begin=500, end=600, complete=1))
        self.status = NS(state=NS(name="charging"), in_cleaning=0, in_returning=0)
        self.entry = NS(entry_id="native-nonclean-entry", options={
            "garage_control_version": 1, "garage_bindings": {"memory-robot": binding()},
        })
        hass.config_entries = MemoryEntries(self.entry)
        async def refresh_summary():
            return None
        self.coordinator = NS(
            duid="memory-robot", config_entry=self.entry, last_update_success=True,
            update_interval=timedelta(seconds=1), data=NS(status=self.status, clean_summary=self.summary),
            properties_api=NS(clean_summary=NS(refresh=refresh_summary), map_content=NS(map_data=NS(charger=NS(x=5, y=5)))),
            resolve_vacuum_position=lambda: NS(trusted=True, x=self.x, y=self.y),
            async_add_listener=lambda _: (lambda: None),
        )
        self.observe("charging", advance=0)
        self.set_door(False)
        async def door_service(call):
            self.commands.append(call.service)
            if call.service != "stop_cover":
                self.set_door(call.service == "open_cover", call.context)
        for service in ("open_cover", "close_cover", "stop_cover"):
            hass.services.async_register("cover", service, door_service)
        self.runtime = module.GarageRuntime(
            hass, self.coordinator, binding(), store=MemorySessionStore(),
            zone_provider=lambda: NS(min_x=0, max_x=10, min_y=0, max_y=10),
            clock=lambda: self.now, utc=lambda: self.utc,
        )
        self.coordinator.garage_runtime = self.runtime

    @callback
    def set_door(self, opened, context=None):
        self.hass.states.async_set("cover.memory_garage", "open" if opened else "closed",
            {"supported_features": 11, "current_position": 100 if opened else 0}, context=context)
        self.hass.states.async_set("binary_sensor.memory_open", "on" if opened else "off", context=context)
        self.hass.states.async_set("binary_sensor.memory_closed", "off" if opened else "on", context=context)

    def observe(self, state, *, in_cleaning=0, in_returning=0, advance=1, position=None):
        self.now += advance
        self.utc += advance
        self.status.state = NS(name=state)
        self.status.in_cleaning = in_cleaning
        self.status.in_returning = in_returning
        if position is not None:
            self.x, self.y = position
        self.coordinator.garage_status_sample = {
            "sampled_at": datetime.fromtimestamp(self.utc, timezone.utc), "state": state,
            "in_cleaning": in_cleaning, "in_returning": in_returning,
        }

    async def initialize(self):
        await self.runtime.async_start()
        self.observe("charging")
        await self.runtime.async_tick()
        await self.hass.async_block_till_done()
        assert self.runtime.session.known_idle
        assert self.commands == []

    async def return_robot(self):
        assert self.runtime.controller.passage_allowed is True
        self.commands.append("robot_charge")
        self.observe("returning_home", in_returning=1, position=(20, 20))
        return "charge-accepted"

    async def finish_return(self):
        self.observe("charging", position=(5, 5))
        await self.runtime.async_tick()
        assert "close_cover" not in self.commands
        self.observe("charging", position=(5, 5))
        await self.runtime.async_tick()
        await self.hass.async_block_till_done()
        assert self.runtime.session.ended
        assert self.runtime.controller.phase == "idle"
        assert self.runtime.controller.reason is None
        assert self.commands[-1] == "close_cover"
        assert self.summary.records == [1]
        assert self.summary.clean_count == 1


def test_native_standalone_charge_closes_after_fresh_return_without_clean_record(caplog):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeRobotBoundary(hass)
            try:
                await rig.initialize()
                assert await rig.runtime.async_send("app_charge", rig.return_robot) == "charge-accepted"
                await hass.async_block_till_done()
                assert rig.runtime.session.task_kind == "return"
                assert rig.runtime.controller.reason is None
                assert rig.commands == ["open_cover", "robot_charge"]
                await rig.finish_return()
                rig.observe("charging", advance=61, position=(5, 5))
                await rig.runtime.async_tick()
                assert rig.runtime.controller.reason is None
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())
    assert not [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


def test_native_goto_idle_does_not_close_then_explicit_charge_can_return(caplog):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeRobotBoundary(hass)
            try:
                await rig.initialize()
                async def goto_robot():
                    assert rig.runtime.controller.passage_allowed is True
                    rig.commands.append("robot_goto")
                    rig.observe("going_to_target", position=(30, 30))
                    return "goto-accepted"
                assert await rig.runtime.async_send("app_goto_target", goto_robot) == "goto-accepted"
                await hass.async_block_till_done()
                assert rig.runtime.session.task_kind == "goto"
                rig.observe("idle", position=(30, 30))
                await rig.runtime.async_tick()
                rig.observe("idle", position=(30, 30))
                await rig.runtime.async_tick()
                await hass.async_block_till_done()
                assert rig.commands == ["open_cover", "robot_goto"]
                assert rig.runtime.controller.reason is None
                assert await rig.runtime.async_send("app_charge", rig.return_robot) == "charge-accepted"
                await hass.async_block_till_done()
                assert rig.runtime.session.task_kind == "return"
                await rig.finish_return()
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())
    assert not [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.parametrize('restart', [False, True])
def test_native_external_task_revokes_old_goto_authority_in_store(restart, caplog):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeRobotBoundary(hass)
            try:
                await rig.initialize()
                async def goto_robot():
                    rig.commands.append('robot_goto')
                    rig.observe('going_to_target', position=(30, 30))
                await rig.runtime.async_send('app_goto_target', goto_robot)
                await hass.async_block_till_done()
                for _ in range(2):
                    rig.observe('idle')
                    await rig.runtime.async_tick()
                assert rig.runtime.session.can_begin
                rig.observe('cleaning', in_cleaning=1)
                await rig.runtime.async_tick()
                rig.observe('idle')
                await rig.runtime.async_tick()
                store = rig.runtime.store
                assert store.get('memory-robot')['correlation_valid'] is False
                assert rig.runtime.controller.phase == 'blocked'
                assert rig.runtime.controller.reason == 'session_recovery_required'
                if restart:
                    rig.runtime.shutdown()
                    await hass.async_block_till_done()
                    rig.runtime = type(rig.runtime)(
                        hass, rig.coordinator, binding(), store=store,
                        zone_provider=lambda: NS(min_x=0, max_x=10, min_y=0, max_y=10),
                        clock=lambda: rig.now, utc=lambda: rig.utc,
                    )
                    rig.coordinator.garage_runtime = rig.runtime
                    await rig.runtime.async_start()
                rig.coordinator.duid_slug = 'memory_robot'
                rig.coordinator.device_info = {'identifiers': {('roborock_plus', 'memory-robot')}}
                entities = importlib.import_module(f'{PACKAGE}.garage_entities')
                problem = next(entity for entity in entities.build_garage_binary_entities([rig.coordinator])
                               if entity._attr_translation_key == 'garage_problem')
                assert problem.is_on is True
                before = list(rig.commands)
                with pytest.raises(Exception, match='session_recovery_required'):
                    rig.runtime.reset()
                with pytest.raises(Exception, match='session_recovery_required'):
                    await rig.runtime.async_send('app_charge', rig.return_robot)
                assert rig.commands == before
                assert not rig.runtime.session.can_close
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())
    assert not [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
