"""Real HA form schemas in memory; no accounts, listeners or physical devices."""

import asyncio
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS

import pytest

from homeassistant.core import HomeAssistant
import voluptuous as vol

from ha_native_garage import MemoryEntries


def test_native_suggestions_required_fields_and_screenshot_recovery():
    async def exercise():
        module = importlib.import_module("custom_components.roborock_plus.config_flow")
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            entry = NS(entry_id="memory-form", options={}, data={}, runtime_data=NS(
                v1=[NS(duid="memory-robot", device=NS(name="Memory robot"))],
            ))
            hass.config_entries = MemoryEntries(entry)
            hass.states.async_set("cover.memory", "closed", {"supported_features": 3})

            class Options(module.RoborockOptionsFlowHandler):
                @property
                def config_entry(self):
                    return entry

            flow = Options(entry)
            flow.hass = hass
            flow.context = {}
            flow.handler = "roborock_plus"
            flow.flow_id = "memory-form-flow"
            page = await flow.async_step_garage_select({"duid": "memory-robot"})
            page = await flow.async_step_garage(page["data_schema"]({
                "enabled": True, "cover_entity_id": "cover.memory",
                "open_source": "timed", "close_source": "contact", "task_door_mode": "after_exit",
            }))
            assert page["step_id"] == "garage_feedback"
            with pytest.raises(vol.Invalid):
                page["data_schema"]({"close_contact": "binary_sensor.memory_closed"})
            page = await flow.async_step_garage_feedback(page["data_schema"]({
                "close_contact": "binary_sensor.memory_closed", "close_contact_state": "off",
            }))
            assert page["step_id"] == "garage_timing"
            markers = {marker.schema: marker for marker in page["data_schema"].schema}
            assert isinstance(markers["open_travel_seconds"], vol.Required)
            assert isinstance(markers["dock_settle_seconds"], vol.Required)
            assert isinstance(markers["allow_estimated"], vol.Optional)
            with pytest.raises(vol.Invalid):
                page["data_schema"]({})
            suggested = page["data_schema"]({"open_travel_seconds": 35})
            assert suggested["travel_margin_seconds"] == 10
            assert suggested["contact_settle_seconds"] == 1
            assert suggested["operation_timeout_seconds"] == 90
            assert suggested["dock_settle_seconds"] == 5
            assert suggested["allow_estimated"] is False
            assert suggested["handover_confirmed"] is False

            denied = await flow.async_step_garage_timing(suggested)
            assert denied["errors"]["base"] == "estimated_risk_not_accepted"
            assert "garage_bindings" not in flow.options

            # The exact screenshot values are accepted as numeric input but
            # rejected by the cross-field safety rule, with visible context.
            attempted = {**suggested, "operation_timeout_seconds": 21,
                         "allow_estimated": True, "handover_confirmed": True}
            denied = await flow.async_step_garage_timing(attempted)
            assert denied["errors"]["base"] == "operation_timeout_too_short"
            assert denied["errors"]["operation_timeout_seconds"] == "operation_timeout_too_short"
            assert denied["description_placeholders"]["current_timeout"] == "21"
            assert denied["description_placeholders"]["required_wait"] == "45"
            assert denied["data_schema"]({})["operation_timeout_seconds"] == 21
            assert "garage_bindings" not in flow.options

            resources = Path(__file__).resolve().parents[1]/"custom_components/roborock_plus"
            for filename in ("strings.json", "translations/en.json", "translations/zh-Hans.json"):
                errors = json.loads((resources/filename).read_text(encoding="utf-8"))["options"]["error"]
                # These are the same substitutions HA's options dialog passes
                # to localize for both its top alert and its field message.
                message = errors[denied["errors"]["base"]].format(**denied["description_placeholders"])
                assert "21" in message and "45" in message

            corrected = denied["data_schema"]({"operation_timeout_seconds": 90})
            saved = await flow.async_step_garage_timing(corrected)
            assert saved["type"] == "create_entry"
            binding = saved["data"]["garage_bindings"]["memory-robot"]
            assert binding["open_travel_seconds"] == 35
            assert binding["operation_timeout_seconds"] == 90
            assert binding["close_contact_state"] == "off"
            assert "close_travel_seconds" not in binding
            assert "position_settle_seconds" not in binding
            await hass.async_block_till_done()
    asyncio.run(exercise())
