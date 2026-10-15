"""UI suggestions must ease setup without granting new runtime authority."""

import asyncio

import pytest

from test_options_ui import _garage_basics, _normal_options, _options_handler


def timing_page(**changes):
    handler = _options_handler()
    handler._garage_duid = "robot-a"
    handler._garage_draft = {
        **_garage_basics(open_source="timed", close_source="contact"),
        "close_contact": "binary_sensor.closed", "close_contact_state": "on", **changes,
    }
    return handler, asyncio.run(handler.async_step_garage_timing())


def defaults(page):
    return {marker.key: marker.default for marker in page["data_schema"]}


def test_new_mixed_source_form_suggests_only_auxiliary_parameters():
    handler, page = timing_page()
    values = defaults(page)
    assert values["travel_margin_seconds"] == 10
    assert values["contact_settle_seconds"] == 1
    assert values["dock_settle_seconds"] == 5
    assert values["operation_timeout_seconds"] == 90
    assert values["open_travel_seconds"] is None
    assert "close_travel_seconds" not in values
    assert "position_settle_seconds" not in values
    assert values["allow_estimated"] is False
    assert values["handover_confirmed"] is False
    # Rendering suggestions does not save settings or fabricate calibration.
    assert "open_travel_seconds" not in handler._garage_values()
    assert "garage_bindings" not in handler.options


def test_timeout_suggestion_uses_only_selected_sources_and_known_values():
    _, page = timing_page(open_travel_seconds=300, close_travel_seconds=600)
    assert defaults(page)["operation_timeout_seconds"] == 340


def test_measured_position_only_suggests_position_and_dock_stability():
    _, page = timing_page(open_source="position", close_source="position", position_kind="measured")
    values = defaults(page)
    assert values["position_settle_seconds"] == 2
    assert values["dock_settle_seconds"] == 5
    assert values["operation_timeout_seconds"] == 90
    assert "travel_margin_seconds" not in values
    assert "open_travel_seconds" not in values
    assert "allow_estimated" not in values


@pytest.mark.parametrize("value", [0, 21, None, "", float("nan")])
def test_ui_does_not_replace_a_supplied_timeout_with_a_recommendation(value):
    handler, page = timing_page(operation_timeout_seconds=value)
    saved_draft = handler._garage_values()["operation_timeout_seconds"]
    assert saved_draft is value
    if value not in (None, ""):
        assert defaults(page)["operation_timeout_seconds"] is value
    else:
        assert defaults(page)["operation_timeout_seconds"] is None


def test_explicit_zero_stabilization_is_preserved():
    _, page = timing_page(contact_settle_seconds=0, travel_margin_seconds=0)
    assert defaults(page)["contact_settle_seconds"] == 0
    assert defaults(page)["travel_margin_seconds"] == 0


def test_disabled_draft_still_saves_without_hardware_or_auxiliary_values():
    handler = _options_handler(_normal_options())
    handler._garage_duid = "robot-a"
    handler._garage_draft = {}
    result = asyncio.run(handler.async_step_garage({"enabled": False}))
    assert result["type"] == "create_entry"
    assert result["data"]["garage_bindings"]["robot-a"] == {"enabled": False}
