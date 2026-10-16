"""Dump real HA options schemas for the offline official-frontend regression.

Run explicitly under HA 2026.9.4 in the isolated dependency container. This only
registers entries in memory; it never sets up the integration or opens a device.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace

from homeassistant.config_entries import ConfigEntries, ConfigEntry, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv


async def dump_schemas():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    flow_module = importlib.import_module("custom_components.roborock_plus.config_flow")
    scenarios = (
        ("fresh", {}),
        ("saved_values", {
            "v1_local_status_poll_interval": 7,
            "v1_map_position_poll_interval": 0,
            "clean_command_watch_timeout": 100,
            "v1_stuck_detection_enabled": False,
            "v1_stuck_window": 160,
            "v1_stuck_radius": 350,
            "show_background": True, "show_rooms": False, "show_walls": False,
            "drawables": {"path": False, "charger": False},
            "garage_guard_enabled": True, "garage_door_entity_id": "cover.retained_legacy",
            "garage_initialized_duids": ["memory-robot"],
            "garage_bindings": {"memory-robot": {"enabled": False}},
        }),
    )
    result = []
    for name, options in scenarios:
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            hass.config_entries = ConfigEntries(hass, {})
            entry = ConfigEntry(
                version=1, minor_version=1, domain="roborock_plus", title="Memory frontend",
                source=SOURCE_USER, data={}, options=options, unique_id=f"frontend-{name}",
                discovery_keys=MappingProxyType({}), subentries_data=(),
            )
            entry.runtime_data = SimpleNamespace(v1=[
                SimpleNamespace(duid="memory-robot", device=SimpleNamespace(name="Memory robot")),
            ])
            hass.config_entries._entries[entry.entry_id] = entry
            flow = flow_module.RoborockOptionsFlowHandler(entry)
            flow.hass = hass
            flow.handler = entry.entry_id
            flow.flow_id = f"frontend-{name}"
            flow.context = {}
            page = await flow.async_step_init()
            assert page["step_id"] == "drawables"
            serialized = cv.to_field_list(page["data_schema"], custom_serializer=cv.custom_serializer)
            assert dict(entry.options) == options
            result.append({"name": name, "schema": serialized, "previous_options": options})
            await hass.async_block_till_done()
    return result


if __name__ == "__main__":
    print(json.dumps(asyncio.run(dump_schemas()), ensure_ascii=False))
