import json
import ast
import asyncio
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FLOW = ROOT / "custom_components" / "roborock_plus" / "config_flow.py"
TRANSLATIONS_DIR = ROOT / "custom_components" / "roborock_plus" / "translations"


def test_status_polling_option_uses_number_box_selector() -> None:
    config_flow = CONFIG_FLOW.read_text(encoding="utf-8")

    assert "NumberSelector(" in config_flow
    assert "NumberSelectorMode.BOX" in config_flow


def test_status_polling_option_description_shows_current_interval() -> None:
    for translation_file in TRANSLATIONS_DIR.glob("*.json"):
        data = json.loads(translation_file.read_text(encoding="utf-8"))
        description = data["options"]["step"]["drawables"]["description"]

        assert "{current_interval}" in description


def test_options_flow_combines_status_polling_and_drawables() -> None:
    config_flow = CONFIG_FLOW.read_text(encoding="utf-8")

    drawables_step = config_flow.split("async def async_step_drawables", 1)[1].split(
        "return self.async_show_form", 1
    )[0]

    assert "CONF_V1_LOCAL_STATUS_POLL_INTERVAL" in drawables_step
    assert "CONF_SHOW_BACKGROUND" in drawables_step
    assert "self.async_create_entry" in drawables_step


def test_options_flow_exposes_garage_guard_fields() -> None:
    config_flow = CONFIG_FLOW.read_text(encoding="utf-8")

    assert "EntitySelector(" in config_flow
    assert "CONF_GARAGE_GUARD_ENABLED" in config_flow
    assert "CONF_GARAGE_DOOR_ENTITY_ID" in config_flow
    assert "CONF_GARAGE_OPEN_SCRIPT_ENTITY_ID" not in config_flow


def test_options_flow_exposes_map_position_interval() -> None:
    config_flow = CONFIG_FLOW.read_text(encoding="utf-8")

    drawables_step = config_flow.split("async def async_step_drawables", 1)[1].split(
        "return self.async_show_form", 1
    )[0]

    assert "CONF_V1_MAP_POSITION_POLL_INTERVAL" in drawables_step
    assert "NumberSelector(" in drawables_step


def test_map_position_interval_description_is_translated() -> None:
    """A safety-relevant option must explain itself in every translation."""
    for translation_file in TRANSLATIONS_DIR.glob("*.json"):
        data = json.loads(translation_file.read_text(encoding="utf-8"))
        step = data["options"]["step"]["drawables"]
        key = "v1_map_position_poll_interval"

        assert key in step["data"], translation_file.name
        assert key in step["data_description"], translation_file.name
        assert "{current_map_interval}" in step["description"], translation_file.name


def test_options_description_shows_both_intervals() -> None:
    for translation_file in TRANSLATIONS_DIR.glob("*.json"):
        data = json.loads(translation_file.read_text(encoding="utf-8"))
        description = data["options"]["step"]["drawables"]["description"]

        assert "{current_interval}" in description, translation_file.name
        assert "{current_map_interval}" in description, translation_file.name


def _options_handler(options=None, robots=("robot-a",)):
    """Execute the actual OptionsFlow class with small native-form substitutes.

    No HA runtime is installed in this repository's unit-test environment. The
    product methods are extracted from their real AST instead of copied here.
    """
    tree = ast.parse(CONFIG_FLOW.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RoborockOptionsFlowHandler")
    scope = {node.id: node.id.removeprefix("CONF_").lower() for node in ast.walk(cls) if isinstance(node, ast.Name) and node.id.isupper()}
    module_path = CONFIG_FLOW.parent / "garage_config.py"
    if module_path.exists():
        spec = spec_from_file_location("garage_options_config", module_path)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        scope.update(vars(module))
    class Marker:
        def __init__(self, key, **kwargs):
            self.key = key
            self.schema = key
            self.default = kwargs.get("default")
    class Base:
        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}
        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}
        def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}
    scope.update({
        "deepcopy": deepcopy,
        "OptionsFlow": Base,
        "vol": SimpleNamespace(Required=Marker, Optional=Marker, Schema=lambda value: value),
        "NumberSelector": lambda config: config,
        "NumberSelectorConfig": dict,
        "NumberSelectorMode": SimpleNamespace(BOX="box"),
        "EntitySelector": lambda config: config,
        "EntitySelectorConfig": dict,
        "SelectSelector": lambda config: config,
        "SelectSelectorConfig": dict,
        "SelectSelectorMode": SimpleNamespace(DROPDOWN="dropdown"),
        "CoverEntityFeature": SimpleNamespace(OPEN=1, CLOSE=2),
        "DOMAIN": "roborock_plus",
        "DRAWABLES": "drawables",
        "DEFAULT_DRAWABLES": {},
        "is_official_cloud_url": lambda _: False,
        "resolve_map_position_intervals": lambda *args, **kwargs: (SimpleNamespace(total_seconds=lambda: 5), None),
    })
    for key in tuple(scope):
        if key.startswith(("MIN_", "MAX_", "DEFAULT_")) and key != "DEFAULT_DRAWABLES":
            scope[key] = 5
    extract = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
    exec(compile(ast.fix_missing_locations(extract), str(CONFIG_FLOW), "exec"), scope)
    entry = SimpleNamespace(
        entry_id="entry-a", options=options or {}, data={},
        runtime_data=SimpleNamespace(v1=[SimpleNamespace(duid=duid, device=SimpleNamespace(name=duid)) for duid in robots]),
    )
    handler = scope["RoborockOptionsFlowHandler"](entry)
    handler.config_entry = entry
    handler.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda _: None),
        config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
    )
    return handler


def _normal_options():
    return {
        "v1_local_status_poll_interval": 3,
        "v1_map_position_poll_interval": 5,
        "clean_command_watch_timeout": 90,
        "v1_stuck_detection_enabled": True,
        "v1_stuck_window": 120,
        "v1_stuck_radius": 200,
        "garage_guard_enabled": False,
        "garage_door_entity_id": "",
        "show_background": False,
        "show_rooms": True,
        "show_walls": True,
        "path": True,
    }


def _valid_garage():
    return {
        "enabled": True, "cover_entity_id": "cover.door", "task_door_mode": "hold_open",
        "open_source": "timed", "close_source": "timed", "open_travel_seconds": 40,
        "close_travel_seconds": 45, "travel_margin_seconds": 1, "contact_settle_seconds": 1,
        "position_settle_seconds": 1, "operation_timeout_seconds": 90, "dock_settle_seconds": 5,
        "allow_estimated": True, "handover_confirmed": True,
    }


def _submit_garage(handler, values):
    """Follow the real native steps using only fields visible on each page."""
    page = asyncio.run(handler.async_step_garage())
    for _ in range(3):
        if page["type"] != "form":
            return page
        visible = {marker.key for marker in page["data_schema"]}
        submitted = {key: value for key, value in values.items() if key in visible}
        method = getattr(handler, f"async_step_{page['step_id']}")
        page = asyncio.run(method(submitted))
        if page.get("errors"):
            return page
    return page


def test_native_garage_entry_preserves_combined_options_and_selects_current_v1():
    handler = _options_handler()
    value = _normal_options()
    value["do_configure_garage"] = True
    result = asyncio.run(handler.async_step_drawables(value))
    assert result["step_id"] == "garage_select"
    assert handler.options["v1_local_status_poll_interval"] == 3
    assert handler.options["drawables"] == {"path": True}
    assert "do_configure_garage" not in handler.options["drawables"]
    result = asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    assert result["step_id"] == "garage"
    result = _submit_garage(handler, _valid_garage())
    assert result["type"] == "create_entry"
    assert result["data"]["garage_control_version"] == 1
    assert result["data"]["garage_bindings"]["robot-a"]["cover_entity_id"] == "cover.door"
    assert result["data"]["drawables"] == {"path": True}


def test_previous_options_submit_still_saves_in_one_page():
    result = asyncio.run(_options_handler().async_step_drawables(_normal_options()))
    assert result["type"] == "create_entry"
    assert result["data"]["drawables"] == {"path": True}


def test_new_native_binding_has_no_implicit_sources_or_safety_durations():
    handler = _options_handler()
    result = asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    assert result["step_id"] == "garage"
    schema = {marker.key: marker for marker in result["data_schema"]}
    assert len(schema) <= 6
    for field in ("open_source", "close_source"):
        assert schema[field].default is None, field
    assert schema["enabled"].default is False
    assert schema["task_door_mode"].default == "hold_open"


def test_incomplete_draft_saves_but_cannot_be_enabled():
    handler = _options_handler()
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    result = _submit_garage(handler, {"enabled": False, "cover_entity_id": "cover.door"})
    assert result["data"]["garage_bindings"]["robot-a"]["enabled"] is False
    result = _submit_garage(handler, {"enabled": True, "cover_entity_id": "cover.door"})
    assert result["type"] == "form"
    assert result["errors"]


def test_native_ui_accepts_after_exit_and_rejects_unsupported_duid_and_cover():
    handler = _options_handler()
    result = asyncio.run(handler.async_step_garage_select({"duid": "q10"}))
    assert result["errors"]["base"] == "unsupported_robot"
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    value = _valid_garage()
    value["task_door_mode"] = "after_exit"
    result = _submit_garage(handler, value)
    assert result["type"] == "create_entry"
    assert result["data"]["garage_bindings"]["robot-a"]["task_door_mode"] == "after_exit"
    handler.hass.states.get = lambda _: SimpleNamespace(state="open", attributes={"supported_features": 4})
    result = _submit_garage(handler, _valid_garage())
    assert "unsupported_cover" in result["errors"].values()


def test_after_exit_uses_existing_form_without_an_unsafe_return_switch():
    handler = _options_handler()
    page = asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    fields = {marker.key for marker in page["data_schema"]}
    assert fields == {"enabled", "cover_entity_id", "task_door_mode", "open_source", "close_source", "close_permission_entity"}


def test_after_exit_description_requires_return_pause_and_hardware_acceptance():
    for path in [CONFIG_FLOW.parent / "strings.json", TRANSLATIONS_DIR / "en.json", TRANSLATIONS_DIR / "zh-Hans.json"]:
        data = json.loads(path.read_text(encoding="utf-8"))
        description = data["options"]["step"]["garage"]["data_description"]["task_door_mode"]
        label = data["selector"]["garage_task_mode"]["options"]["after_exit"]
        if path.name == "zh-Hans.json":
            assert "暂停" in description and "确认" in description and "实测" in description
            assert "不支持" not in label
        else:
            assert "pause" in description.lower() and "confirm" in description.lower()
            assert "hardware" in description.lower()
            assert "unsupported" not in label.lower()
        assert "app_resume" in description


def test_native_ui_rejects_same_door_cross_entry_and_other_legacy_robot():
    handler = _options_handler(robots=("robot-a", "robot-b"))
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    other = SimpleNamespace(entry_id="other", options={"garage_bindings": {"other-robot": _valid_garage()}}, runtime_data=SimpleNamespace(v1=[]))
    handler.hass.config_entries.async_entries = lambda _: [handler.config_entry, other]
    result = _submit_garage(handler, _valid_garage())
    assert "duplicate_binding" in result["errors"].values()
    handler.options.update({"garage_guard_enabled": True, "garage_door_entity_id": "cover.door"})
    handler.hass.config_entries.async_entries = lambda _: [handler.config_entry]
    result = _submit_garage(handler, _valid_garage())
    assert "legacy_guard_conflict" in result["errors"].values()


def test_legacy_options_edit_cannot_steal_a_new_binding_door():
    handler = _options_handler()
    other = SimpleNamespace(entry_id="other", options={"garage_bindings": {"other-robot": _valid_garage()}}, runtime_data=SimpleNamespace(v1=[]))
    handler.hass.config_entries.async_entries = lambda _: [handler.config_entry, other]
    value = _normal_options()
    value.update({"garage_guard_enabled": True, "garage_door_entity_id": "cover.door"})
    result = asyncio.run(handler.async_step_drawables(value))
    assert result["type"] == "form"
    assert result["errors"]["base"] == "legacy_guard_conflict"


def test_safety_configuration_fields_and_errors_are_translated():
    for path in [CONFIG_FLOW.parent / "strings.json", TRANSLATIONS_DIR / "en.json", TRANSLATIONS_DIR / "zh-Hans.json"]:
        data = json.loads(path.read_text(encoding="utf-8"))["options"]
        steps = [data["step"][key] for key in ("garage", "garage_feedback", "garage_timing")]
        labels = {key: value for step in steps for key, value in step["data"].items()}
        descriptions = {key: value for step in steps for key, value in step["data_description"].items()}
        for key in (*_valid_garage(), "open_contact", "close_contact", "open_contact_state", "close_contact_state", "position_kind", "open_position", "close_position", "close_permission_entity"):
            assert labels.get(key), (path.name, key)
            assert descriptions.get(key), (path.name, key)
        assert data["step"]["drawables"]["data"]["do_configure_garage"]
        assert data["step"]["garage_select"]["data"]["duid"]
        assert data["error"]["unsupported_return_interlock"]
        assert data["error"]["legacy_guard_conflict"]


def _page_keys(page):
    return {marker.key for marker in page["data_schema"]}


def _garage_basics(**changes):
    values = {key: value for key, value in _valid_garage().items() if key in {
        "enabled", "cover_entity_id", "task_door_mode", "open_source", "close_source",
    }}
    values.update(changes)
    return values


def test_timed_source_skips_feedback_and_only_shows_estimate_timing():
    handler = _options_handler()
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    page = asyncio.run(handler.async_step_garage(_garage_basics()))
    assert page["step_id"] == "garage_timing"
    assert _page_keys(page) == {"open_travel_seconds", "close_travel_seconds", "travel_margin_seconds", "operation_timeout_seconds", "dock_settle_seconds", "allow_estimated", "handover_confirmed"}
    for marker in page["data_schema"]:
        if marker.key in ("open_travel_seconds", "close_travel_seconds"):
            assert marker.default is None
        elif marker.key.endswith("seconds"):
            assert marker.default == {"travel_margin_seconds": 10, "operation_timeout_seconds": 90, "dock_settle_seconds": 5}[marker.key]
    values = {key: value for key, value in _valid_garage().items() if key in _page_keys(page)}
    result = asyncio.run(handler.async_step_garage_timing(values))
    saved = result["data"]["garage_bindings"]["robot-a"]
    assert "contact_settle_seconds" not in saved
    assert "position_settle_seconds" not in saved


def test_contact_source_requires_polarity_then_only_contact_stabilization():
    handler = _options_handler()
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    page = asyncio.run(handler.async_step_garage(_garage_basics(open_source="contact", close_source="contact")))
    assert page["step_id"] == "garage_feedback"
    assert _page_keys(page) == {"open_contact", "close_contact", "open_contact_state", "close_contact_state"}
    assert all(marker.default is None for marker in page["data_schema"])
    missing = asyncio.run(handler.async_step_garage_feedback({}))
    assert missing["errors"]
    assert missing["step_id"] == "garage_feedback"
    page = asyncio.run(handler.async_step_garage_feedback({"open_contact": "binary_sensor.open", "close_contact": "binary_sensor.closed", "open_contact_state": "off", "close_contact_state": "on"}))
    assert page["step_id"] == "garage_timing"
    assert _page_keys(page) == {"contact_settle_seconds", "operation_timeout_seconds", "dock_settle_seconds", "handover_confirmed"}


@pytest.mark.parametrize("kind,extra", [("measured", set()), ("estimated", {"open_travel_seconds", "close_travel_seconds", "travel_margin_seconds", "allow_estimated"})])
def test_position_feedback_kind_controls_visible_timing(kind, extra):
    handler = _options_handler()
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    page = asyncio.run(handler.async_step_garage(_garage_basics(open_source="position", close_source="position")))
    assert _page_keys(page) == {"position_kind", "open_position", "close_position"}
    page = asyncio.run(handler.async_step_garage_feedback({"position_kind": kind, "open_position": 95, "close_position": 5}))
    assert _page_keys(page) == {"position_settle_seconds", "operation_timeout_seconds", "dock_settle_seconds", "handover_confirmed"} | extra


def test_source_switch_and_reentry_preserve_inactive_hardware_values():
    original = _valid_garage()
    handler = _options_handler({"garage_control_version": 1, "garage_bindings": {"robot-a": original}})
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    contact_values = {
        **_garage_basics(open_source="contact", close_source="contact"),
        "open_contact": "binary_sensor.open", "close_contact": "binary_sensor.closed",
        "open_contact_state": "off", "close_contact_state": "on", "contact_settle_seconds": 2,
    }
    result = _submit_garage(handler, contact_values)
    saved = result["data"]
    changed = saved["garage_bindings"]["robot-a"]
    assert changed["open_travel_seconds"] == original["open_travel_seconds"]
    assert changed["close_travel_seconds"] == original["close_travel_seconds"]
    assert changed["position_settle_seconds"] == original["position_settle_seconds"]
    again = _options_handler(saved)
    asyncio.run(again.async_step_garage_select({"duid": "robot-a"}))
    page = asyncio.run(again.async_step_garage(_garage_basics(open_source="contact", close_source="contact")))
    defaults = {marker.key: marker.default for marker in page["data_schema"]}
    assert defaults["open_contact_state"] == "off"
    result = _submit_garage(again, _garage_basics())
    restored = result["data"]["garage_bindings"]["robot-a"]
    assert restored["open_contact"] == "binary_sensor.open"
    assert restored["open_contact_state"] == "off"
    assert restored["contact_settle_seconds"] == 2


@pytest.mark.parametrize("field,invalid,corrected,step", [
    ("position_settle_seconds", float("nan"), 2, "garage_timing"),
    ("open_contact_state", "closed", "off", "garage_feedback"),
])
def test_invalid_retained_hidden_value_is_visible_and_can_be_corrected(field, invalid, corrected, step):
    original = {**_valid_garage(), field: invalid}
    handler = _options_handler({"garage_control_version": 1, "garage_bindings": {"robot-a": original}})
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    page = asyncio.run(handler.async_step_garage(_garage_basics()))
    assert page["step_id"] == step
    assert page["errors"].get(field)
    assert field in _page_keys(page)
    page = asyncio.run(getattr(handler, f"async_step_{step}")({field: corrected}))
    if page["type"] == "form":
        page = asyncio.run(handler.async_step_garage_timing({}))
    assert page["type"] == "create_entry"
    assert page["data"]["garage_bindings"]["robot-a"][field] == corrected


@pytest.mark.parametrize("field,invalid,reason", [
    ("cover_entity_id", "switch.wrong", "invalid_entity_reference"),
    ("open_source", "auto", "invalid_evidence_source"),
    ("task_door_mode", "unsafe_mode", "invalid_task_door_mode"),
    ("close_permission_entity", "sensor.wrong", "invalid_entity_reference"),
])
def test_disabled_native_draft_rejects_supplied_invalid_fields_without_discarding_sources(field, invalid, reason):
    handler = _options_handler()
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    draft = {**_garage_basics(enabled=False), field: invalid}
    page = asyncio.run(handler.async_step_garage(draft))
    assert page["type"] == "form"
    assert page["errors"][field] == reason
    assert handler._garage_values()["open_source"] == draft["open_source"]
    assert handler._garage_values()["close_source"] == "timed"
