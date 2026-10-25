"""Required values must have named feedback instead of a frontend dead end."""

import asyncio
from copy import deepcopy
import json

import pytest

from test_options_ui import CONFIG_FLOW, _normal_options, _options_handler


NUMBERS = (
    "v1_local_status_poll_interval", "v1_map_position_poll_interval",
    "clean_command_watch_timeout", "v1_stuck_window", "v1_stuck_radius",
)


def payload():
    flat = _normal_options()
    monitoring = {key: flat.pop(key) for key in (*NUMBERS, "v1_stuck_detection_enabled")}
    return {"do_configure_garage": False, "monitoring": monitoring, "map_display": flat}


def test_page_has_no_empty_section_defaults_or_legacy_ui_fields():
    handler = _options_handler()
    page = asyncio.run(handler.async_step_init())
    assert [marker.key for marker in page["data_schema"]] == ["do_configure_garage", "monitoring", "map_display"]
    for marker, validator in page["data_schema"].items():
        if hasattr(validator, "schema"):
            assert marker.default is None
            assert not {"garage_guard_enabled", "garage_door_entity_id"} & {child.key for child in validator.schema}


@pytest.mark.parametrize("field", NUMBERS)
@pytest.mark.parametrize("blank", [None, "", "omitted"])
def test_blank_field_is_named_and_cannot_save(field, blank):
    handler = _options_handler()
    submitted = payload()
    if blank == "omitted":
        submitted["monitoring"].pop(field)
    else:
        submitted["monitoring"][field] = blank
    submitted["map_display"]["show_rooms"] = False
    submitted["do_configure_garage"] = True
    page = asyncio.run(handler.async_step_drawables(submitted))
    assert page["type"] == "form"
    assert page["errors"]["base"] == "missing_monitoring_fields"
    assert page["errors"][f"monitoring.{field}"] == "required_field"
    labels = json.loads((CONFIG_FLOW.parent / "translations/zh-Hans.json").read_text(encoding="utf-8"))["options"]["step"]["drawables"]["sections"]["monitoring"]["data"]
    assert labels[field] in page["description_placeholders"]["fields"]
    groups = {marker.key: value for marker, value in page["data_schema"].items()}
    numeric_markers = {marker.key: marker for marker in groups["monitoring"].schema}
    assert numeric_markers[field].default in (None, "")
    assert numeric_markers[field].description["suggested_value"] in (None, "")
    map_markers = {marker.key: marker for marker in groups["map_display"].schema}
    assert map_markers["show_rooms"].default is False
    assert next(marker for marker in page["data_schema"] if marker.key == "do_configure_garage").default is True
    assert handler.config_entry.options == {}


@pytest.mark.parametrize("field,value", [
    ("v1_local_status_poll_interval", 0), ("v1_local_status_poll_interval", 61),
    ("v1_map_position_poll_interval", -1), ("v1_map_position_poll_interval", 301),
    ("clean_command_watch_timeout", -1), ("clean_command_watch_timeout", 601),
    ("v1_stuck_window", 29), ("v1_stuck_window", 601),
    ("v1_stuck_radius", 49), ("v1_stuck_radius", 2001),
    ("v1_stuck_radius", 200.5), ("v1_local_status_poll_interval", True),
    ("v1_local_status_poll_interval", float("nan")), ("v1_local_status_poll_interval", float("inf")),
    ("v1_local_status_poll_interval", "broken"),
])
def test_invalid_number_is_rejected_before_options_persistence(field, value):
    handler = _options_handler()
    submitted = payload()
    submitted["monitoring"][field] = value
    page = asyncio.run(handler.async_step_drawables(submitted))
    assert page["type"] == "form"
    assert page["errors"]["base"] == "invalid_monitoring_fields"
    assert page["errors"][f"monitoring.{field}"] == "invalid_monitoring_number"
    assert handler.config_entry.options == {}


def test_multiple_missing_fields_are_all_listed():
    handler = _options_handler()
    submitted = payload()
    submitted["monitoring"][NUMBERS[0]] = None
    submitted["monitoring"][NUMBERS[3]] = ""
    page = asyncio.run(handler.async_step_drawables(submitted))
    labels = json.loads((CONFIG_FLOW.parent / "translations/zh-Hans.json").read_text(encoding="utf-8"))["options"]["step"]["drawables"]["sections"]["monitoring"]["data"]
    assert labels[NUMBERS[0]] in page["description_placeholders"]["fields"]
    assert labels[NUMBERS[3]] in page["description_placeholders"]["fields"]


def test_unrelated_saved_values_survive_a_new_payload_and_valid_zero_false():
    """A form that does not mention a stored key must not erase it.

    ``retained`` stands in for any option the flow does not render; the flat
    garage-guard pair used to be the real-world case and is now gone.
    """
    previous = {"retained": 42}
    handler = _options_handler(deepcopy(previous))
    submitted = payload()
    submitted["monitoring"].update({"v1_map_position_poll_interval": 0, "clean_command_watch_timeout": 0, "v1_stuck_detection_enabled": False})
    saved = asyncio.run(handler.async_step_drawables(submitted))
    assert saved["type"] == "create_entry"
    for key, value in previous.items():
        assert saved["data"][key] == value
    assert saved["data"]["clean_command_watch_timeout"] == 0
    assert saved["data"]["v1_map_position_poll_interval"] == 0
    assert saved["data"]["v1_stuck_detection_enabled"] is False
    assert handler.config_entry.options == previous
