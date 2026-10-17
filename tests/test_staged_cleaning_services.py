"""The stage entity service returns an atomic receipt without bypassing runtime."""

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[1] / "custom_components/roborock_plus"


class ControlError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class HAError(Exception):
    def __init__(self, **kwargs):
        self.reason = kwargs["translation_placeholders"]["reason"]
        super().__init__(self.reason)


def entity_class(class_name="RoborockVacuum"):
    tree = ast.parse((ROOT / "vacuum.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    method = next((node for node in cls.body if isinstance(node, ast.AsyncFunctionDef)
                   and node.name == "async_start_garage_cleaning_stage"), None)
    assert method is not None, "all selectable vacuum protocols need an explicit stage-service boundary"

    class Transport:
        async def send(self, command, params=None):
            self.calls.append((command, params))
            return {"accepted": True}

    namespace = {
        "Transport": Transport, "Any": Any, "ServiceResponse": dict,
        "GarageControlError": ControlError, "GarageTaskError": ControlError,
        "ServiceValidationError": HAError, "HomeAssistantError": HAError, "DOMAIN": "roborock_plus",
    }
    wrapper = ast.ClassDef(name=class_name, bases=[ast.Name(id="Transport", ctx=ast.Load())],
                           keywords=[], body=[method], decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                 str(ROOT / "vacuum.py"), "exec"), namespace)
    return namespace[class_name]


def test_stage_entity_forwards_exact_stage_and_receipt_to_its_runtime():
    stage = {"command": "app_start", "params": None, "settings": []}
    previous = {"runtime_id": "runtime", "session_id": "previous", "interruption_revision": 0}
    receipt = {**previous, "session_id": "current"}
    received = []

    async def start_stage(value, send, *, previous_receipt=None):
        received.append((value, previous_receipt))
        await send("set_mop_mode", [303])
        await send(value["command"], value["params"])
        return receipt

    entity = entity_class()()
    entity.calls = []
    entity.coordinator = NS(garage_runtime=NS(async_start_stage=start_stage))
    result = asyncio.run(entity.async_start_garage_cleaning_stage(stage, previous))
    assert result == receipt
    assert received == [(stage, previous)]
    assert entity.calls == [("set_mop_mode", [303]), ("app_start", None)]


@pytest.mark.parametrize("reason", ["stage_interrupted", "stage_receipt_mismatch", "invalid_stage"])
def test_stage_service_rejections_are_explicit_home_assistant_errors(reason):
    async def reject(*args, **kwargs):
        raise ControlError(reason)

    entity = entity_class()()
    entity.coordinator = NS(garage_runtime=NS(async_start_stage=reject))
    with pytest.raises(HAError, match=reason):
        asyncio.run(entity.async_start_garage_cleaning_stage({"command": "app_start"}))


def test_stage_service_requires_a_configured_runtime():
    entity = entity_class()()
    entity.coordinator = NS(garage_runtime=None, garage_setup_error=None)
    with pytest.raises(HAError, match="garage_not_configured"):
        asyncio.run(entity.async_start_garage_cleaning_stage({"command": "app_start"}))


@pytest.mark.parametrize("class_name", ["RoborockQ7Vacuum", "RoborockQ10Vacuum"])
def test_non_v1_protocols_reject_stages_explicitly_without_transport(class_name):
    entity = entity_class(class_name)()
    entity.calls = []
    with pytest.raises(HAError, match="unsupported_protocol"):
        asyncio.run(entity.async_start_garage_cleaning_stage({"command": "app_start"}))
    assert entity.calls == []


def test_unexpected_transport_error_keeps_its_cause_and_visible_detail():
    failure = RuntimeError("settings rejected")

    async def reject(*args, **kwargs):
        raise failure

    entity = entity_class()()
    entity.coordinator = NS(garage_runtime=NS(async_start_stage=reject))
    with pytest.raises(HAError, match="command_failed.*settings rejected") as caught:
        asyncio.run(entity.async_start_garage_cleaning_stage({"command": "app_start"}))
    assert caught.value.__cause__ is failure


def test_stage_service_has_schema_target_response_and_translated_fields():
    services = yaml.safe_load((ROOT / "services.yaml").read_text(encoding="utf-8"))
    assert "start_garage_cleaning_stage" in services
    stage = services["start_garage_cleaning_stage"]
    assert stage["target"]["entity"]["domain"] == "vacuum"
    assert stage["target"]["entity"]["integration"] == "roborock_plus"
    assert stage["fields"]["stage"]["required"] is True
    assert stage["fields"]["stage"]["selector"] == {"object": {}}
    for name in ("strings.json", "translations/en.json", "translations/zh-Hans.json"):
        data = json.loads((ROOT / name).read_text(encoding="utf-8"))
        translated = data["services"]["start_garage_cleaning_stage"]
        assert translated["name"] and translated["description"]
        for field in ("stage", "previous_receipt"):
            assert translated["fields"][field]["name"]


def test_stage_registration_requires_service_response():
    tree = ast.parse((ROOT / "services.py").read_text(encoding="utf-8"))
    registrations = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute)
                     and node.func.attr == "async_register_platform_entity_service"]
    matching = [node for node in registrations if any(
        kw.arg == "func" and isinstance(kw.value, ast.Constant)
        and kw.value.value == "async_start_garage_cleaning_stage" for kw in node.keywords
    )]
    assert len(matching) == 1
    response = next(kw.value for kw in matching[0].keywords if kw.arg == "supports_response")
    assert isinstance(response, ast.Attribute) and response.attr == "ONLY"
