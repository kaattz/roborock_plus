"""Opt-in migration exercises the actual validators and runtime routing gates."""

import asyncio
from copy import deepcopy
import importlib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/roborock_plus"


@pytest.fixture
def modules():
    package = ModuleType("_garage_migration_test_package")
    package.__path__ = [str(COMPONENT)]
    sys.modules.setdefault(package.__name__, package)
    return (
        importlib.import_module(package.__name__ + ".garage_config"),
        importlib.import_module(package.__name__ + ".garage_runtime"),
    )


def binding(**changes):
    value = dict(
        enabled=True, cover_entity_id="cover.garage", open_source="timed", close_source="timed",
        open_travel_seconds=45, close_travel_seconds=40, travel_margin_seconds=2,
        contact_settle_seconds=1, position_settle_seconds=1, operation_timeout_seconds=90,
        dock_settle_seconds=5, allow_estimated=True, handover_confirmed=True,
    )
    value.update(changes)
    return value


def coordinator(options, duid="robot-a"):
    return NS(duid=duid, config_entry=NS(options=options))


def test_old_configuration_keeps_legacy_routing_without_creating_a_binding(modules):
    config, runtime = modules
    options = {"garage_guard_enabled": True, "garage_door_entity_id": "cover.garage", "drawables": {"path": True}}
    before = deepcopy(options)
    assert config.resolve_binding(options, "robot-a") is None
    assert runtime.configured(coordinator(options)) is False
    runtime.check_legacy_ownership(NS(data={}), options, "robot-a")
    assert options == before
    assert "garage_bindings" not in options
    assert "garage_control_version" not in options


def test_disabled_incomplete_draft_does_not_take_over_the_old_guard(modules):
    config, runtime = modules
    options = {
        "garage_guard_enabled": True, "garage_door_entity_id": "cover.garage",
        "garage_control_version": 1,
        "garage_bindings": {"robot-a": {"enabled": False, "cover_entity_id": "cover.garage"}},
    }
    assert config.resolve_binding(options, "robot-a") is None
    assert runtime.configured(coordinator(options)) is False
    config.validate_binding_ownership(options["garage_bindings"]["robot-a"], duid="robot-a", entry_id="entry", entries=[("entry", options, ("robot-a", "robot-b"))])
    runtime.check_legacy_ownership(NS(data={}), options, "robot-a")


def test_explicitly_enabled_binding_defaults_to_hold_open_and_single_robot_can_forward(modules):
    config, runtime = modules
    options = {
        "garage_control_version": 1, "garage_bindings": {"robot-a": binding()},
        "garage_guard_enabled": True, "garage_door_entity_id": "cover.garage",
    }
    resolved = config.resolve_binding(options, "robot-a")
    assert resolved["task_door_mode"] == "hold_open"
    assert runtime.configured(coordinator(options)) is True
    config.validate_binding_ownership(resolved, duid="robot-a", entry_id="entry", entries=[("entry", options, ("robot-a",))])
    owner = NS(coordinator=coordinator(options))
    hass = NS(data={runtime.REGISTRY_KEY: {"cover.garage": owner}})
    runtime.check_legacy_ownership(hass, options, "robot-a")


def test_the_retired_legacy_pair_carries_no_authority_any_more(modules):
    """A leftover flat-guard pair must not decide ownership or block anything.

    It used to act as a second owner. That guard is removed, so the same options
    now pass both the binding-ownership check and the (inert) legacy helper: the
    native bindings are the only authority left.
    """
    config, runtime = modules
    options = {
        "garage_control_version": 1, "garage_bindings": {"robot-a": binding()},
        "garage_guard_enabled": True, "garage_door_entity_id": "cover.garage",
    }
    config.validate_binding_ownership(
        binding(), duid="robot-a", entry_id="entry",
        entries=[("entry", options, ("robot-a", "robot-b"))])
    owner = NS(coordinator=coordinator(options))
    hass = NS(data={runtime.REGISTRY_KEY: {"cover.garage": owner}})
    runtime.check_legacy_ownership(hass, options, "robot-b")


def test_different_native_doors_can_replace_the_old_global_guard_per_robot(modules):
    config, runtime = modules
    options = {
        "garage_control_version": 1,
        "garage_bindings": {"robot-a": binding(), "robot-b": binding(cover_entity_id="cover.other")},
        "garage_guard_enabled": True, "garage_door_entity_id": "cover.garage",
    }
    entries = [("entry", options, ("robot-a", "robot-b"))]
    for duid in ("robot-a", "robot-b"):
        config.validate_binding_ownership(config.resolve_binding(options, duid), duid=duid, entry_id="entry", entries=entries)
        assert runtime.configured(coordinator(options, duid)) is True
    config.validate_legacy_guard_ownership(options, entry_id="entry", entries=entries)


def test_options_save_preserves_initialization_markers_and_other_robot_binding(modules):
    """Reuse the native-options boundary harness, which executes real flow AST."""
    config, _ = modules
    path = ROOT / "tests/test_options_ui.py"
    spec = spec_from_file_location("_garage_migration_options_harness", path)
    harness = module_from_spec(spec)
    spec.loader.exec_module(harness)
    options = {
        "garage_control_version": 1,
        "garage_initialized_duids": ["robot-a"],
        "garage_bindings": {"robot-a": binding(), "robot-b": {"enabled": False}},
    }
    handler = harness._options_handler(options)
    result = asyncio.run(handler.async_step_drawables(harness._normal_options()))
    saved = result["data"]
    assert saved["garage_initialized_duids"] == ["robot-a"]
    assert saved["garage_bindings"]["robot-b"] == {"enabled": False}
    assert config.resolve_binding(saved, "robot-a")["task_door_mode"] == "hold_open"
    assert saved["drawables"] == {"path": True}


def test_bad_enabled_binding_cannot_silently_return_to_the_legacy_path(modules):
    config, runtime = modules
    options = {"garage_control_version": 1, "garage_guard_enabled": True, "garage_door_entity_id": "cover.garage", "garage_bindings": {"robot-a": {"enabled": True}}}
    assert runtime.configured(coordinator(options)) is True
    with pytest.raises(config.GarageConfigError):
        config.resolve_binding(options, "robot-a")
