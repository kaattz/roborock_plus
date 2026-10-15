"""Garage form regressions execute the real OptionsFlow methods via its harness."""

import asyncio
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


@pytest.fixture
def harness():
    path = Path(__file__).with_name("test_options_ui.py")
    spec = spec_from_file_location("_garage_feedback_options_harness", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _timing_handler(harness):
    handler = harness._options_handler()
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    page = asyncio.run(handler.async_step_garage({
        "enabled": True, "cover_entity_id": "cover.door", "task_door_mode": "after_exit",
        "open_source": "timed", "close_source": "contact",
    }))
    assert page["step_id"] == "garage_feedback"
    page = asyncio.run(handler.async_step_garage_feedback({
        "close_contact": "binary_sensor.closed", "close_contact_state": "off",
    }))
    assert page["step_id"] == "garage_timing"
    assert not page["errors"]
    return handler


def _screenshot_timing(**changes):
    value = {
        "open_travel_seconds": 35, "travel_margin_seconds": 10,
        "contact_settle_seconds": 1, "operation_timeout_seconds": 21,
        "allow_estimated": True, "handover_confirmed": True,
    }
    value.update(changes)
    return value


def _defaults(page):
    return {marker.key: marker.default for marker in page["data_schema"]}


def _assert_dual_error(page, field, reason):
    assert page["type"] == "form"
    assert page["errors"][field] == reason
    assert page["errors"].get("base"), "Rejected form needs a visible error at its top"


def test_missing_dock_is_reported_before_timeout_and_keeps_entered_timing(harness):
    handler = _timing_handler(harness)
    entered = _screenshot_timing()
    page = asyncio.run(handler.async_step_garage_timing(entered))
    assert page["step_id"] == "garage_timing"
    _assert_dual_error(page, "dock_settle_seconds", "missing_safety_configuration")
    defaults = _defaults(page)
    assert defaults["dock_settle_seconds"] == 5
    for key, value in entered.items():
        assert defaults[key] == value
    assert "garage_bindings" not in handler.options


def test_low_timeout_is_visible_after_dock_is_filled_and_correction_saves(harness):
    handler = _timing_handler(harness)
    entered = _screenshot_timing(dock_settle_seconds=5)
    page = asyncio.run(handler.async_step_garage_timing(entered))
    _assert_dual_error(page, "operation_timeout_seconds", "operation_timeout_too_short")
    assert page["description_placeholders"]["current_timeout"] == "21"
    assert page["description_placeholders"]["required_wait"] == "45"
    for key, value in entered.items():
        assert _defaults(page)[key] == value
    result = asyncio.run(handler.async_step_garage_timing({"operation_timeout_seconds": 46}))
    assert result["type"] == "create_entry"
    saved = result["data"]["garage_bindings"]["robot-a"]
    assert saved["task_door_mode"] == "after_exit"
    assert saved["open_source"] == "timed"
    assert saved["close_source"] == "contact"
    assert saved["close_contact_state"] == "off"
    assert saved["open_travel_seconds"] == 35
    assert saved["travel_margin_seconds"] == 10
    assert saved["dock_settle_seconds"] == 5


def test_missing_contact_polarity_has_top_error_and_preserves_selected_feedback(harness):
    handler = harness._options_handler()
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    asyncio.run(handler.async_step_garage({
        "enabled": True, "cover_entity_id": "cover.door", "task_door_mode": "after_exit",
        "open_source": "timed", "close_source": "contact",
    }))
    page = asyncio.run(handler.async_step_garage_feedback({"close_contact": "binary_sensor.closed"}))
    assert page["step_id"] == "garage_feedback"
    _assert_dual_error(page, "close_contact", "invalid_contact_configuration")
    assert _defaults(page)["close_contact"] == "binary_sensor.closed"
    assert _defaults(page)["close_contact_state"] is None
    assert handler._garage_values()["open_source"] == "timed"
    assert handler._garage_values()["close_source"] == "contact"


def test_handover_rejection_has_top_error_and_does_not_discard_timing(harness):
    handler = _timing_handler(harness)
    entered = _screenshot_timing(
        dock_settle_seconds=5, operation_timeout_seconds=46, handover_confirmed=False,
    )
    page = asyncio.run(handler.async_step_garage_timing(entered))
    _assert_dual_error(page, "handover_confirmed", "handover_required")
    for key, value in entered.items():
        assert _defaults(page)[key] == value
    assert "garage_bindings" not in handler.options
