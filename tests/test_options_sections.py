"""Exercise the actual grouped options methods without a physical HA host."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

from test_options_ui import CONFIG_FLOW, _normal_options, _options_handler, _valid_garage


MONITORING = {
    "v1_local_status_poll_interval", "v1_map_position_poll_interval",
    "clean_command_watch_timeout", "v1_stuck_detection_enabled",
    "v1_stuck_window", "v1_stuck_radius",
}
LEGACY = {"garage_guard_enabled", "garage_door_entity_id"}
GROUPS = {"monitoring", "map_display", "legacy_compatibility"}


def grouped(values):
    values = deepcopy(values)
    navigation = values.pop("do_configure_garage", False)
    return {
        "do_configure_garage": navigation,
        "monitoring": {key: values.pop(key) for key in MONITORING},
        "legacy_compatibility": {key: values.pop(key) for key in LEGACY},
        "map_display": values,
    }


def fields(page):
    return {marker.key: value for marker, value in page["data_schema"].items()}


def section_defaults(page, group):
    return {marker.key: marker.default for marker in fields(page)[group].schema}


def test_root_has_new_garage_entry_and_native_collapsible_groups():
    page = asyncio.run(_options_handler().async_step_init())
    root = fields(page)
    assert list(root) == ["do_configure_garage", "monitoring", "map_display", "legacy_compatibility"]
    assert root["monitoring"].options["collapsed"] is False
    assert root["map_display"].options["collapsed"] is True
    assert root["legacy_compatibility"].options["collapsed"] is True
    assert set(section_defaults(page, "monitoring")) == MONITORING
    assert set(section_defaults(page, "legacy_compatibility")) == LEGACY
    assert set(section_defaults(page, "map_display")) == {"show_background", "show_rooms", "show_walls"}


def test_grouped_submit_preserves_existing_values_and_original_storage_shape():
    previous = {
        "unrelated": {"preserved": True}, "garage_bindings": {"robot-z": {"enabled": False}},
        "garage_initialized_duids": ["robot-z"], "drawables": {"existing_overlay": True},
    }
    handler = _options_handler(previous)
    values = _normal_options()
    values.update({"clean_command_watch_timeout": 0, "v1_stuck_detection_enabled": False,
                   "show_rooms": False, "path": False, "garage_guard_enabled": True,
                   "garage_door_entity_id": "cover.legacy"})
    submitted = grouped(values)
    original_payload = deepcopy(submitted)
    result = asyncio.run(handler.async_step_drawables(submitted))
    assert result["type"] == "create_entry"
    saved = result["data"]
    assert saved["clean_command_watch_timeout"] == 0
    assert saved["v1_stuck_detection_enabled"] is False
    assert saved["show_rooms"] is False
    assert saved["garage_guard_enabled"] is True
    assert saved["garage_door_entity_id"] == "cover.legacy"
    assert saved["drawables"] == {"existing_overlay": True, "path": False}
    assert saved["garage_bindings"] == previous["garage_bindings"]
    assert saved["garage_initialized_duids"] == ["robot-z"]
    assert saved["unrelated"] == {"preserved": True}
    assert not (GROUPS | {"do_configure_garage"}) & saved.keys()
    assert not (GROUPS | {"do_configure_garage"}) & saved["drawables"].keys()
    assert submitted == original_payload
    assert handler.config_entry.options == previous


def test_old_flat_submit_remains_compatible_with_grouped_submit():
    values = _normal_options()
    flat = asyncio.run(_options_handler().async_step_drawables(values))
    nested = asyncio.run(_options_handler().async_step_drawables(grouped(values)))
    assert nested["data"] == flat["data"]


def test_grouped_new_garage_navigation_keeps_options_pending_until_final_save():
    handler = _options_handler()
    values = {**_normal_options(), "do_configure_garage": True}
    result = asyncio.run(handler.async_step_drawables(grouped(values)))
    assert result["step_id"] == "garage_select"
    assert handler.options["drawables"] == {"path": True}
    assert handler.config_entry.options == {}
    assert not GROUPS & handler.options.keys()


def test_legacy_conflict_remains_visible_and_preserves_grouped_edits():
    handler = _options_handler()
    other = SimpleNamespace(
        entry_id="other", options={"garage_bindings": {"other-robot": _valid_garage()}},
        runtime_data=SimpleNamespace(v1=[]),
    )
    handler.hass.config_entries.async_entries = lambda _: [handler.config_entry, other]
    values = {**_normal_options(), "garage_guard_enabled": True,
              "garage_door_entity_id": "cover.door", "show_rooms": False,
              "do_configure_garage": True}
    page = asyncio.run(handler.async_step_drawables(grouped(values)))
    assert page["type"] == "form"
    assert page["errors"]["base"] == "legacy_guard_conflict"
    assert section_defaults(page, "legacy_compatibility")["garage_door_entity_id"] == "cover.door"
    assert section_defaults(page, "monitoring")["v1_local_status_poll_interval"] == 3
    assert section_defaults(page, "map_display")["show_rooms"] is False
    checkbox = next(marker for marker in page["data_schema"] if marker.key == "do_configure_garage")
    assert checkbox.default is True
    assert handler.config_entry.options == {}


def test_group_field_translations_follow_the_native_frontend_lookup_path():
    for name in ("strings.json", "translations/en.json", "translations/zh-Hans.json"):
        step = json.loads((CONFIG_FLOW.parent / name).read_text(encoding="utf-8"))["options"]["step"]["drawables"]
        assert set(step["sections"]) == GROUPS
        assert set(step["sections"]["monitoring"]["data"]) == MONITORING
        assert set(step["sections"]["legacy_compatibility"]["data"]) == LEGACY
        all_group_fields = set()
        for group in GROUPS:
            translated = step["sections"][group]
            assert translated["name"] and translated["description"]
            assert set(translated["data"]) == set(translated["data_description"])
            assert not all_group_fields & translated["data"].keys()
            all_group_fields.update(translated["data"])
            for field, label in translated["data"].items():
                assert label == step["data"][field]
                assert translated["data_description"][field] == step["data_description"][field]
                if name.endswith("zh-Hans.json"):
                    assert any("\u4e00" <= char <= "\u9fff" for char in label), field
        assert all_group_fields == set(step["data"]) - {"do_configure_garage"}
        assert "{current_interval}" in step["description"]
        assert "{current_map_interval}" in step["description"]
