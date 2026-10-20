"""Native HA section serialization and real options completion in memory."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace

import pytest

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section
from homeassistant.helpers.data_entry_flow import FlowManagerIndexView
from homeassistant.helpers.translation import async_get_translations
import voluptuous as vol

from ha_native_garage import options_schema_input


PACKAGE = "custom_components.roborock_plus"
MONITORING_NUMBERS = (
    "v1_local_status_poll_interval", "v1_map_position_poll_interval",
    "clean_command_watch_timeout", "v1_stuck_window", "v1_stuck_radius",
)


async def native_translations(hass, language):
    """Read actual custom-component resources without integration setup."""
    loader.async_setup(hass)
    path = Path(__file__).resolve().parents[1]/"custom_components/roborock_plus"
    integration = loader.Integration(
        hass, PACKAGE, path, json.loads((path/"manifest.json").read_text()),
        {"translations"},
    )
    hass.data[loader.DATA_CUSTOM_COMPONENTS] = {"roborock_plus": integration}
    hass.config.language = language
    return await async_get_translations(hass, language, "options", ["roborock_plus"])


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
            translations = await native_translations(hass, "zh-Hans")
            entry = make_entry(previous_options())
            hass.config_entries._entries[entry.entry_id] = entry
            flow = make_flow(hass, entry)
            page = await flow.async_step_init()
            schema = page["data_schema"]
            sections = {
                marker.schema: validator for marker, validator in schema.schema.items()
                if isinstance(validator, section)
            }
            assert set(sections) == {"monitoring", "map_display"}
            serialized = FlowManagerIndexView(None)._prepare_result_json(page)["data_schema"]
            assert serialized[0]["name"] == "do_configure_garage"
            groups = {field["name"]: field for field in serialized if field["type"] == "expandable"}
            assert groups["monitoring"]["expanded"] is True
            assert groups["map_display"]["expanded"] is False
            assert all("default" not in group for group in groups.values())
            all_names = {field["name"] for group in groups.values() for field in group["schema"]}
            assert not {"garage_guard_enabled", "garage_door_entity_id"} & all_names
            monitoring = {marker.schema: marker for marker in sections["monitoring"].schema.schema}
            assert all(isinstance(monitoring[key], vol.Optional) for key in MONITORING_NUMBERS)
            prefix = "component.roborock_plus.options.step.drawables.sections.monitoring.data."
            assert all("必填" in translations[prefix+key] for key in MONITORING_NUMBERS)
            assert {"show_background", "show_rooms", "show_walls", "path"} <= {
                field["name"] for field in groups["map_display"]["schema"]
            }
            assert dict(entry.options) == previous_options()
    asyncio.run(exercise())


@pytest.mark.parametrize("case", ["missing", "null", "empty", "range"])
def test_real_options_manager_keeps_missing_and_null_until_user_corrects_them(monkeypatch, case):
    async def exercise():
        integration = importlib.import_module(PACKAGE)
        from homeassistant import config_entries

        async def dependencies_available(*args):
            # Loading the real options handler must not boot HTTP/frontend.
            # All Python dependencies are already installed in this container.
            return None

        monkeypatch.setattr(config_entries, "async_process_deps_reqs", dependencies_available)
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            hass.config_entries = ConfigEntries(hass, {})
            await native_translations(hass, "zh-Hans")
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
            page = await hass.config_entries.options.async_init(entry.entry_id)
            assert page["type"] is FlowResultType.FORM
            assert page["step_id"] == "drawables"
            submitted = options_schema_input(page["data_schema"], {
                "clean_command_watch_timeout": 0, "v1_stuck_detection_enabled": False,
                "show_rooms": False, "path": False,
            })
            if case == "missing":
                submitted["monitoring"].pop("v1_local_status_poll_interval")
            elif case == "null":
                submitted["monitoring"]["v1_local_status_poll_interval"] = None
            elif case == "empty":
                submitted["monitoring"]["v1_local_status_poll_interval"] = ""
            else:
                submitted["monitoring"]["v1_local_status_poll_interval"] = 61
            # Send the actual raw API payload, without calling schema() again:
            # doing so could hide a missing field by applying its old default.
            denied = await hass.config_entries.options.async_configure(page["flow_id"], submitted)
            assert denied["type"] is FlowResultType.FORM
            assert denied["step_id"] == "drawables"
            assert denied["errors"]["base"] == (
                "invalid_monitoring_fields" if case == "range" else "missing_monitoring_fields"
            )
            assert denied["errors"]["monitoring.v1_local_status_poll_interval"] == (
                "invalid_monitoring_number" if case == "range" else "required_field"
            )
            assert dict(entry.options) == previous
            assert reloads == []

            corrected = deepcopy(submitted)
            corrected["monitoring"].update(v1_local_status_poll_interval=4, v1_stuck_radius=250)
            finished = await hass.config_entries.options.async_configure(denied["flow_id"], corrected)
            await hass.async_block_till_done()
            assert finished["type"] is FlowResultType.CREATE_ENTRY
            assert entry.options["v1_local_status_poll_interval"] == 4
            assert entry.options["v1_stuck_radius"] == 250
            assert entry.options["clean_command_watch_timeout"] == 0
            assert entry.options["v1_stuck_detection_enabled"] is False
            assert entry.options["show_rooms"] is False
            assert entry.options["drawables"]["path"] is False
            assert entry.options["garage_guard_enabled"] == previous["garage_guard_enabled"]
            assert entry.options["garage_door_entity_id"] == previous["garage_door_entity_id"]
            assert reloads == [entry.entry_id]
            remove_listener()
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
            assert set(submitted) == {"do_configure_garage", "monitoring", "map_display"}
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
            # Already-open older forms can still submit flat legacy options.
            submitted = {
                key: value for key, value in previous.items()
                if key not in ("drawables", "garage_bindings", "garage_initialized_duids", "retained_option")
            }
            submitted.update({
                "garage_guard_enabled": True, "garage_door_entity_id": "cover.owned",
                "clean_command_watch_timeout": 0, "v1_stuck_detection_enabled": False,
                "show_rooms": False, "path": False, "do_configure_garage": True,
            })
            denied = await flow.async_step_drawables(submitted)
            assert denied["type"] is FlowResultType.FORM
            assert denied["errors"]["base"] == "legacy_guard_conflict"
            assert dict(entry.options) == previous
            reflected = options_schema_input(denied["data_schema"], {})
            assert reflected["do_configure_garage"] is True
            assert reflected["monitoring"]["clean_command_watch_timeout"] == 0
            assert reflected["monitoring"]["v1_stuck_detection_enabled"] is False
            assert reflected["map_display"]["show_rooms"] is False
            assert reflected["map_display"]["path"] is False
            assert "legacy_compatibility" not in reflected
            assert flow.options["garage_door_entity_id"] == "cover.owned"
            assert flow.options["garage_guard_enabled"] is True
    asyncio.run(exercise())


@pytest.mark.parametrize("case,reason,expected_fields", [
    ("missing_one", "missing_monitoring_fields", ("v1_local_status_poll_interval",)),
    ("missing_all", "missing_monitoring_fields", MONITORING_NUMBERS),
    ("null_two", "missing_monitoring_fields", ("v1_local_status_poll_interval", "v1_stuck_radius")),
    ("empty", "missing_monitoring_fields", ("v1_stuck_window",)),
    ("fraction", "invalid_monitoring_fields", ("v1_local_status_poll_interval",)),
    ("boolean", "invalid_monitoring_fields", ("v1_stuck_radius",)),
    ("range_two", "invalid_monitoring_fields", ("v1_local_status_poll_interval", "v1_stuck_radius")),
])
@pytest.mark.parametrize("language", ["zh-Hans", "en"])
def test_native_backend_monitoring_errors_are_localized_complete_and_preserve_input(case, reason, expected_fields, language):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            hass.config_entries = ConfigEntries(hass, {})
            translations = await native_translations(hass, language)
            previous = previous_options()
            entry = make_entry(previous)
            hass.config_entries._entries[entry.entry_id] = entry
            flow = make_flow(hass, entry)
            page = await flow.async_step_init()
            submitted = options_schema_input(page["data_schema"], {
                "clean_command_watch_timeout": 0, "v1_stuck_detection_enabled": False,
                "show_rooms": False, "path": False, "do_configure_garage": True,
            })
            monitoring = submitted["monitoring"]
            if case == "missing_one":
                monitoring.pop("v1_local_status_poll_interval")
            elif case == "missing_all":
                for key in MONITORING_NUMBERS:
                    monitoring.pop(key)
            elif case == "null_two":
                monitoring.update(v1_local_status_poll_interval=None, v1_stuck_radius=None)
            elif case == "empty":
                monitoring["v1_stuck_window"] = ""
            elif case == "fraction":
                monitoring["v1_local_status_poll_interval"] = 1.5
            elif case == "boolean":
                monitoring["v1_stuck_radius"] = True
            else:
                monitoring.update(v1_local_status_poll_interval=61, v1_stuck_radius=49)
            denied = await flow.async_step_drawables(submitted)
            assert denied["type"] is FlowResultType.FORM
            assert denied["errors"]["base"] == reason
            message = translations[f"component.roborock_plus.options.error.{reason}"].format(
                **denied["description_placeholders"]
            )
            prefix = "component.roborock_plus.options.step.drawables.sections.monitoring.data."
            for key in expected_fields:
                assert translations[prefix+key] in denied["description_placeholders"]["fields"]
                assert translations[prefix+key] in message
                assert key not in message
            reflected_markers = {
                marker.schema: validator for marker, validator in denied["data_schema"].schema.items()
            }
            reflected_numbers = {
                marker.schema: marker for marker in reflected_markers["monitoring"].schema.schema
            }
            def default(marker):
                description = getattr(marker, "description", None) or {}
                if "suggested_value" in description:
                    return description["suggested_value"]
                return vol.UNDEFINED if marker.default is vol.UNDEFINED else marker.default()
            for key in expected_fields:
                shown = default(reflected_numbers[key])
                if key not in monitoring or monitoring[key] in (None, ""):
                    assert shown in (vol.UNDEFINED, None, "")
                elif isinstance(monitoring[key], bool):
                    # A numeric widget cannot safely represent a boolean;
                    # render it empty while preserving the explicit error.
                    assert shown in (vol.UNDEFINED, None, "")
                else:
                    assert shown == monitoring[key]
            reflected_map = {
                marker.schema: marker for marker in reflected_markers["map_display"].schema.schema
            }
            root_markers = {marker.schema: marker for marker in denied["data_schema"].schema}
            assert default(root_markers["do_configure_garage"]) is True
            assert default(reflected_map["show_rooms"]) is False
            assert default(reflected_map["path"]) is False
            assert default(reflected_numbers["v1_stuck_detection_enabled"]) is False
            assert dict(entry.options) == previous
    asyncio.run(exercise())
