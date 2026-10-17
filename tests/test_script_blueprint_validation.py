"""Script blueprints keep the same structural checks as automation blueprints."""

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("script_blueprint_validator", ROOT / "scripts/validate_blueprints.py")
validator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = validator
spec.loader.exec_module(validator)


def validate(tmp_path, body):
    path = tmp_path / "fixture.yaml"
    path.write_text(body, encoding="utf-8")
    return validator.validate(path)


def test_native_script_blueprint_shape_is_supported(tmp_path):
    assert validate(tmp_path, "blueprint:\n  name: Test\n  domain: script\nsequence:\n  - stop: Done\n") == []


@pytest.mark.parametrize("body,expected", [
    ("blueprint: {name: Test, domain: script}\nactions: []\n", "sequence"),
    ("blueprint: {name: Test, domain: script}\nsequence: {}\n", "list"),
    ("blueprint: {name: Test, domain: script}\nsequence:\n  - if: []\n", "then"),
    ("blueprint: {name: Test, domain: script}\nsequence:\n  - repeat: {count: 2}\n", "sequence"),
    ("blueprint: {name: Test, domain: script}\nsequence:\n  - action: !input missing\n", "not declared"),
    ("blueprint: {name: Test, domain: unsupported}\nsequence: []\n", "domain"),
    ("blueprint: {name: Test, domain: automation}\nsequence: []\n", "triggers"),
])
def test_invalid_script_and_automation_shapes_remain_rejected(tmp_path, body, expected):
    assert any(expected in problem for problem in validate(tmp_path, body))


def test_original_automation_blueprint_still_validates():
    assert validator.validate(ROOT / "blueprints/扫地机分区定时清扫.yaml") == []


def test_new_staged_script_blueprint_validates():
    assert validator.validate(ROOT / "blueprints/多阶段清扫.yaml") == []
