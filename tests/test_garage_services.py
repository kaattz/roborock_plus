"""Exercise real native service registration without dispatching device calls."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import yaml


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/roborock_plus"


def test_garage_services_register_on_vacuum_without_force_schema():
    path = COMPONENT / "services.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    tree.body.insert(0, ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0))
    calls = []
    scope = {
        "DOMAIN": "roborock_plus", "VACUUM_DOMAIN": "vacuum", "callback": lambda fn: fn,
        "SupportsResponse": SimpleNamespace(ONLY="only", NONE="none"),
        "service": SimpleNamespace(async_register_platform_entity_service=lambda *args, **kwargs: calls.append((args, kwargs))),
        "cv": SimpleNamespace(make_entity_service_schema=lambda value: value),
        "vol": SimpleNamespace(Required=lambda value: value, Optional=lambda value: value,
                               In=lambda value: value, Coerce=lambda value: value),
    }
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), scope)
    scope["async_setup_services"](object())
    registered = {args[2]: kwargs for args, kwargs in calls}
    for name in ("request_garage_close", "reset_garage_control"):
        assert name in registered
        assert registered[name]["entity_domain"] == "vacuum"
        assert registered[name]["schema"] is None
        assert registered[name]["func"] == f"async_{name}"
        assert registered[name]["supports_response"] == "none"


def test_garage_service_descriptions_target_vacuum_and_have_no_force_field():
    data = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    for name in ("request_garage_close", "reset_garage_control"):
        assert data[name]["target"]["entity"]["domain"] == "vacuum"
        assert data[name]["target"]["entity"]["integration"] == "roborock_plus"
        assert not data[name].get("fields")


def test_garage_services_and_failure_reasons_have_translations():
    for name in ("strings.json", "translations/en.json", "translations/zh-Hans.json"):
        data = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        for service in ("request_garage_close", "reset_garage_control"):
            assert data["services"][service]["name"]
            assert data["services"][service]["description"]
        assert "{reason}" in data["exceptions"]["garage_control_error"]["message"]
