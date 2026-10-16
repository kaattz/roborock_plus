"""Native HA section serialization and real options completion in memory."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace

from homeassistant.config_entries import ConfigEntries, ConfigEntry, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section
from homeassistant.helpers import config_validation as cv

from ha_native_garage import options_schema_input


PACKAGE = "custom_components.roborock_plus"


def previous_options():
    return {
        "v1_local_status_poll_interval": 3, "v1_map_position_poll_interval": 5,
        "clean_command_watch_timeout": 90, "v1_stuck_detection_enabled": True,
        "v1_stuck_window": 120, "v1_stuck_radius": 200,
        "garage_guard_enabled": True, "garage_door_entity_id": "cover.previous",
        "show_background": True, "show_rooms": True, "show_walls": True,
        "drawables": {"path": True}, "retained_option": "kept",
        "garage_initialized_duids": ["memory-robot"],
        "garage_bindings": {"memory-robot": {"enabled": False}},
    }


def make_entry(options, *, unique_id="memory-sections"):
    entry = ConfigEntry(
        version=1, minor_version=1, domain="roborock_plus", title="Memory sections",
        source=SOURCE_USER, data={}, options=options, unique_id=unique_id,
        discovery_keys=MappingProxyType({}), subentries_data=(),
    )
    entry.runtime_data = SimpleNamespace(v1=[
        SimpleNamespace(duid="memory-robot", device=SimpleNamespace(name="Memory robot")),
    ])
    return entry


def make_flow(hass, entry):
    module = importlib.import_module(f"{PACKAGE}.config_flow")
    flow = module.RoborockOptionsFlowHandler(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "isolated-options-sections"
    flow.context = {}
    return flow


def test_native_home_sections_have_real_expandable_serializer_and_collapse_state():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            hass.config_entries = ConfigEntries(hass, {})
            entry = make_entry(previous_options())
            hass.config_entries._entries[entry.entry_id] = entry
            flow = make_flow(hass, entry)
            page = await flow.async_step_init()
            schema = page["data_schema"]
            sections = {
                marker.schema: validator for marker, validator in schema.schema.items()
                if isinstance(validator, section)
            }
            assert set(sections) == {"monitoring", "map_display", "legacy_compatibility"}
            serialized = cv.to_field_list(schema, custom_serializer=cv.custom_serializer)
            assert serialized[0]["name"] == "do_configure_garage"
            groups = {field["name"]: field for field in serialized if field["type"] == "expandable"}
            assert groups["monitoring"]["expanded"] is True
            assert groups["map_display"]["expanded"] is False
            assert groups["legacy_compatibility"]["expanded"] is False
            assert {field["name"] for field in groups["legacy_compatibility"]["schema"]} == {
                "garage_guard_enabled", "garage_door_entity_id",
            }
            assert {"show_background", "show_rooms", "show_walls", "path"} <= {
                field["name"] for field in groups["map_display"]["schema"]
            }
            assert dict(entry.options) == previous_options()
    asyncio.run(exercise())


def test_grouped_finish_preserves_legacy_and_zero_false_without_nested_persistence():
    async def exercise():
        integration = importlib.import_module(PACKAGE)
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            hass.config_entries = ConfigEntries(hass, {})
            previous = previous_options()
            entry = make_entry(previous)
            hass.config_entries._entries[entry.entry_id] = entry
            reloads = []
            async def reload_boundary(entry_id):
                reloads.append(entry_id)
                return True
            hass.config_entries.async_reload = reload_boundary
            hass.data["roborock_plus_options_snapshot"] = {
                entry.entry_id: {key: deepcopy(value) for key, value in previous.items()
                                 if key != "garage_initialized_duids"},
            }
            remove_listener = entry.add_update_listener(integration._async_options_updated)
            flow = make_flow(hass, entry)
            page = await flow.async_step_init()
            submitted = options_schema_input(page["data_schema"], {
                "clean_command_watch_timeout": 0, "v1_stuck_detection_enabled": False,
                "show_rooms": False, "path": False,
            })
            assert set(submitted) == {"do_configure_garage", "monitoring", "map_display", "legacy_compatibility"}
            result = await flow.async_step_drawables(submitted)
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert reloads == [] and dict(entry.options) == previous
            await hass.config_entries.options.async_finish_flow(flow, result)
            await hass.async_block_till_done()
            saved = dict(entry.options)
            assert saved["clean_command_watch_timeout"] == 0
            assert saved["v1_stuck_detection_enabled"] is False
            assert saved["show_rooms"] is False and saved["drawables"]["path"] is False
            for key in ("garage_guard_enabled", "garage_door_entity_id", "garage_bindings",
                        "garage_initialized_duids", "retained_option", "show_background", "show_walls",
                        "v1_local_status_poll_interval", "v1_map_position_poll_interval"):
                assert saved[key] == previous[key]
            assert not ({"monitoring", "map_display", "legacy_compatibility", "do_configure_garage"} & saved.keys())
            assert not ({"monitoring", "map_display", "legacy_compatibility"} & saved["drawables"].keys())
            assert reloads == [entry.entry_id]
            await hass.config_entries.options.async_finish_flow(flow, result)
            await hass.async_block_till_done()
            assert reloads == [entry.entry_id]
            metadata = {**result, "data": deepcopy(saved)}
            metadata["data"]["garage_initialized_duids"].append("another-robot")
            await hass.config_entries.options.async_finish_flow(flow, metadata)
            await hass.async_block_till_done()
            assert entry.options["garage_initialized_duids"] == ["memory-robot", "another-robot"]
            assert reloads == [entry.entry_id]
            remove_listener()
    asyncio.run(exercise())


def test_grouped_legacy_ownership_error_preserves_monitoring_and_map_edits():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            hass.config_entries = ConfigEntries(hass, {})
            previous = previous_options()
            previous["garage_guard_enabled"] = False
            entry = make_entry(previous)
            owner = make_entry({"garage_bindings": {"owner-robot": {
                "enabled": True, "cover_entity_id": "cover.owned",
            }}}, unique_id="other-owner")
            hass.config_entries._entries[entry.entry_id] = entry
            hass.config_entries._entries[owner.entry_id] = owner
            flow = make_flow(hass, entry)
            page = await flow.async_step_init()
            submitted = options_schema_input(page["data_schema"], {
                "garage_guard_enabled": True, "garage_door_entity_id": "cover.owned",
                "clean_command_watch_timeout": 0, "v1_stuck_detection_enabled": False,
                "show_rooms": False, "path": False, "do_configure_garage": True,
            })
            denied = await flow.async_step_drawables(submitted)
            assert denied["type"] is FlowResultType.FORM
            assert denied["errors"]["base"] == "legacy_guard_conflict"
            assert dict(entry.options) == previous
            reflected = denied["data_schema"]({})
            assert reflected["do_configure_garage"] is True
            assert reflected["monitoring"]["clean_command_watch_timeout"] == 0
            assert reflected["monitoring"]["v1_stuck_detection_enabled"] is False
            assert reflected["map_display"]["show_rooms"] is False
            assert reflected["map_display"]["path"] is False
            assert reflected["legacy_compatibility"]["garage_door_entity_id"] == "cover.owned"
    asyncio.run(exercise())
