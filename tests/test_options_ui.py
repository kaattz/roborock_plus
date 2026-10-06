import json
from pathlib import Path


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
