"""Real HA options completion with its existing config-entry update listener.

Run explicitly in the isolated Linux HA dependency environment. The entry is
registered in memory without setup; only the reload boundary is replaced, so no
account, integration setup, network connection, or device action is possible.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace

import pytest

from homeassistant.config_entries import ConfigEntries, ConfigEntry, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType


PACKAGE = "custom_components.roborock_plus"


@pytest.mark.parametrize("route", ["polling_drawables", "garage_enabled", "garage_disabled_draft"])
def test_real_options_manager_finishes_with_one_existing_listener_reload(route):
    async def exercise():
        integration = importlib.import_module(PACKAGE)
        flow_module = importlib.import_module(f"{PACKAGE}.config_flow")
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            hass.config_entries = ConfigEntries(hass, {})
            previous = {
                "garage_initialized_duids": ["previous-robot"],
                "garage_bindings": {"previous-robot": {"enabled": False}},
                "retained_option": "kept",
            }
            entry = ConfigEntry(
                version=1, minor_version=1, domain="roborock_plus", title="Memory robot",
                source=SOURCE_USER, data={}, options=previous, unique_id="memory-account",
                discovery_keys=MappingProxyType({}), subentries_data=(),
            )
            entry.runtime_data = SimpleNamespace(v1=[
                SimpleNamespace(duid="memory-robot", device=SimpleNamespace(name="Memory robot")),
            ])
            # Register without ConfigEntries.async_add: that API also sets up
            # the integration and could attempt account access.
            hass.config_entries._entries[entry.entry_id] = entry
            reload_requests = []

            async def memory_reload(entry_id):
                reload_requests.append(entry_id)
                return True

            hass.config_entries.async_reload = memory_reload
            hass.data["roborock_plus_options_snapshot"] = {entry.entry_id: deepcopy(previous)}
            listener_calls = []

            async def existing_listener(listener_hass, listener_entry):
                listener_calls.append(deepcopy(dict(listener_entry.options)))
                await integration._async_options_updated(listener_hass, listener_entry)

            remove_listener = entry.add_update_listener(existing_listener)
            assert len(entry.update_listeners) == 1
            flow = flow_module.RoborockOptionsFlowHandler(entry)
            flow.hass = hass
            flow.handler = entry.entry_id
            flow.flow_id = "isolated-options-finish"
            flow.context = {}
            page = await flow.async_step_init()
            ordinary = page["data_schema"]({
                "v1_local_status_poll_interval": 3, "v1_map_position_poll_interval": 5,
                "clean_command_watch_timeout": 90, "v1_stuck_detection_enabled": True,
                "v1_stuck_window": 120, "v1_stuck_radius": 200,
                "garage_guard_enabled": False, "garage_door_entity_id": "cover.memory_garage",
                "show_background": False, "show_rooms": True, "show_walls": True,
                "path": False,
                "do_configure_garage": route != "polling_drawables",
            })
            page = await flow.async_step_drawables(ordinary)
            if route != "polling_drawables":
                assert page["step_id"] == "garage_select"
                page = await flow.async_step_garage_select(
                    page["data_schema"]({"duid": "memory-robot"})
                )
                values = {
                    "enabled": route == "garage_enabled", "cover_entity_id": "cover.memory_garage",
                    "task_door_mode": "after_exit",
                }
                if values["enabled"]:
                    values.update({
                        "open_source": "contact", "close_source": "contact",
                        "open_contact": "binary_sensor.memory_open", "open_contact_state": "on",
                        "close_contact": "binary_sensor.memory_closed", "close_contact_state": "off",
                        "contact_settle_seconds": 1, "operation_timeout_seconds": 3,
                        "dock_settle_seconds": 5, "handover_confirmed": True,
                    })
                visited = set()
                while page["type"] is FlowResultType.FORM:
                    assert not page["errors"]
                    assert page["step_id"] not in visited
                    visited.add(page["step_id"])
                    fields = {marker.schema for marker in page["data_schema"].schema}
                    submitted = page["data_schema"]({key: value for key, value in values.items() if key in fields})
                    page = await getattr(flow, f"async_step_{page['step_id']}")(submitted)
                assert visited == (
                    {"garage", "garage_feedback", "garage_timing"}
                    if values["enabled"] else {"garage"}
                )
            assert page["type"] is FlowResultType.CREATE_ENTRY
            assert dict(entry.options) == previous
            assert listener_calls == reload_requests == []

            result = await hass.config_entries.options.async_finish_flow(flow, page)
            await hass.async_block_till_done()
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert dict(entry.options) == result["data"]
            assert entry.options["v1_local_status_poll_interval"] == 3
            assert entry.options["show_rooms"] is True
            assert entry.options["show_background"] is False
            assert entry.options["drawables"]["path"] is False
            assert entry.options["retained_option"] == "kept"
            assert entry.options["garage_initialized_duids"] == ["previous-robot"]
            assert entry.options["garage_bindings"]["previous-robot"] == {"enabled": False}
            if route != "polling_drawables":
                saved = entry.options["garage_bindings"]["memory-robot"]
                assert saved["enabled"] is (route == "garage_enabled")
                assert saved["task_door_mode"] == "after_exit"
                if route == "garage_disabled_draft":
                    assert "open_source" not in saved
            assert listener_calls == [dict(entry.options)]
            assert reload_requests == [entry.entry_id]

            # Saving the same options again must not fire the listener twice.
            await hass.config_entries.options.async_finish_flow(flow, page)
            await hass.async_block_till_done()
            assert len(listener_calls) == 1
            assert reload_requests == [entry.entry_id]

            # The existing listener deliberately ignores journal-initialization
            # metadata while HA still writes it into the entry's options.
            metadata_result = {**page, "data": deepcopy(dict(entry.options))}
            metadata_result["data"]["garage_initialized_duids"] = ["previous-robot", "memory-robot"]
            await hass.config_entries.options.async_finish_flow(flow, metadata_result)
            await hass.async_block_till_done()
            assert entry.options["garage_initialized_duids"] == ["previous-robot", "memory-robot"]
            assert len(listener_calls) == 2
            assert reload_requests == [entry.entry_id]
            remove_listener()

    asyncio.run(exercise())
