"""Tests for Manual (custom server URL) region support."""

import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = ROOT / "custom_components" / "roborock_plus"
CONFIG_FLOW = COMPONENT_DIR / "config_flow.py"
STRINGS = COMPONENT_DIR / "strings.json"
CONST = COMPONENT_DIR / "const.py"
TRANSLATIONS_DIR = COMPONENT_DIR / "translations"


def _load(name: str, path: Path):
    """Load a component module that has no Home Assistant imports."""
    spec = spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SERVER_URL = _load("rp_server_url_under_test", COMPONENT_DIR / "server_url.py")
is_valid_server_url = SERVER_URL.is_valid_server_url
normalize_server_url = SERVER_URL.normalize_server_url

REGION_OPTIONS = [
    "auto",
    "us",
    "eu",
    "ru",
    "cn",
    "custom",
]
REGION_CUSTOM = "custom"


def test_custom_region_is_selectable() -> None:
    const_source = CONST.read_text(encoding="utf-8")

    assert 'REGION_CUSTOM = "custom"' in const_source
    assert "REGION_CUSTOM," in const_source


def test_custom_region_does_not_build_a_cloud_url() -> None:
    """`custom` must never be interpolated into the regional cloud URL."""
    config_flow = CONFIG_FLOW.read_text(encoding="utf-8")

    assert "if region == REGION_CUSTOM:" in config_flow
    # The branch has to run before the regional URL is built.
    assert config_flow.index("if region == REGION_CUSTOM:") < config_flow.index(
        "iot.roborock.com"
    )


def test_config_flow_exposes_custom_url_step() -> None:
    config_flow = CONFIG_FLOW.read_text(encoding="utf-8")

    assert "async def async_step_custom_url(" in config_flow
    assert "CONF_ROBOROCK_SERVER_URL" in config_flow
    assert "invalid_url_format" in config_flow
    assert "TextSelectorType.URL" in config_flow
    assert "is_valid_server_url(" in config_flow


def test_server_url_validation_accepts_local_server() -> None:
    assert is_valid_server_url("https://api-rr.example.com:555")
    assert is_valid_server_url("http://192.168.1.10:555")
    assert is_valid_server_url("  https://api-rr.example.com:555  ")


def test_server_url_validation_rejects_bad_input() -> None:
    for bad_url in ("", "api-rr.example.com", "ftp://api-rr.example.com", "https://"):
        assert not is_valid_server_url(bad_url), bad_url


def test_server_url_validation_rejects_non_strings() -> None:
    for bad_value in (None, 123, ["https://api-rr.example.com"]):
        assert not is_valid_server_url(bad_value), bad_value


def test_normalize_server_url_trims_whitespace() -> None:
    assert normalize_server_url("  https://api-rr.example.com:555 ") == (
        "https://api-rr.example.com:555"
    )


def test_manual_label_present_in_every_translation() -> None:
    for path in TRANSLATIONS_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        options = data["selector"]["region"]["options"]

        assert "custom" in options, path.name
        assert options["custom"], path.name
        # Every selectable region must stay labelled.
        assert set(options) == set(REGION_OPTIONS), path.name


def test_strings_json_documents_custom_url_step() -> None:
    data = json.loads(STRINGS.read_text(encoding="utf-8"))

    assert "invalid_url_format" in data["config"]["error"]
    assert "custom_url" in data["config"]["step"]
    assert "roborock_server_url" in data["config"]["step"]["custom_url"]["data"]


def test_en_translation_documents_custom_url_step() -> None:
    data = json.loads((TRANSLATIONS_DIR / "en.json").read_text(encoding="utf-8"))

    assert "invalid_url_format" in data["config"]["error"]
    assert "custom_url" in data["config"]["step"]
    assert "roborock_server_url" in data["config"]["step"]["custom_url"]["data"]
