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
        "OptionsFlowWithReload": Base,
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
    result = asyncio.run(handler.async_step_garage(_valid_garage()))
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
    for field in ("open_source", "close_source", "open_contact_state", "close_contact_state", "open_travel_seconds", "close_travel_seconds", "travel_margin_seconds", "contact_settle_seconds", "position_settle_seconds", "operation_timeout_seconds", "dock_settle_seconds"):
        assert schema[field].default is None, field
    assert schema["handover_confirmed"].default is False
    assert schema["enabled"].default is False


def test_incomplete_draft_saves_but_cannot_be_enabled():
    handler = _options_handler()
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    result = asyncio.run(handler.async_step_garage({"enabled": False, "cover_entity_id": "cover.door"}))
    assert result["data"]["garage_bindings"]["robot-a"]["enabled"] is False
    result = asyncio.run(handler.async_step_garage({"enabled": True, "cover_entity_id": "cover.door"}))
    assert result["type"] == "form"
    assert result["errors"]


def test_native_ui_rejects_unsupported_duid_position_capability_and_after_exit():
    handler = _options_handler()
    result = asyncio.run(handler.async_step_garage_select({"duid": "q10"}))
    assert result["errors"]["base"] == "unsupported_robot"
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    value = _valid_garage()
    value["task_door_mode"] = "after_exit"
    result = asyncio.run(handler.async_step_garage(value))
    assert "unsupported_return_interlock" in result["errors"].values()
    handler.hass.states.get = lambda _: SimpleNamespace(state="open", attributes={"supported_features": 4})
    result = asyncio.run(handler.async_step_garage(_valid_garage()))
    assert "unsupported_cover" in result["errors"].values()


def test_native_ui_rejects_same_door_cross_entry_and_other_legacy_robot():
    handler = _options_handler(robots=("robot-a", "robot-b"))
    asyncio.run(handler.async_step_garage_select({"duid": "robot-a"}))
    other = SimpleNamespace(entry_id="other", options={"garage_bindings": {"other-robot": _valid_garage()}}, runtime_data=SimpleNamespace(v1=[]))
    handler.hass.config_entries.async_entries = lambda _: [handler.config_entry, other]
    result = asyncio.run(handler.async_step_garage(_valid_garage()))
    assert "duplicate_binding" in result["errors"].values()
    handler.options.update({"garage_guard_enabled": True, "garage_door_entity_id": "cover.door"})
    handler.hass.config_entries.async_entries = lambda _: [handler.config_entry]
    result = asyncio.run(handler.async_step_garage(_valid_garage()))
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
        step = data["step"]["garage"]
        for key in (*_valid_garage(), "open_contact", "close_contact", "open_contact_state", "close_contact_state", "position_kind", "open_position", "close_position", "close_permission_entity"):
            assert step["data"].get(key), (path.name, key)
            assert step["data_description"].get(key), (path.name, key)
        assert data["step"]["drawables"]["data"]["do_configure_garage"]
        assert data["step"]["garage_select"]["data"]["duid"]
        assert data["error"]["unsupported_return_interlock"]
        assert data["error"]["legacy_guard_conflict"]
