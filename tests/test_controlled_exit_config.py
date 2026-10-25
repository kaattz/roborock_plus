"""Controlled exit must be off unless the operator asks for it in full.

The feature drives the robot out of the cabinet on its own before a clean
command. That is a real physical behaviour change, so:

* it is off by default and changes nothing unless enabled;
* enabling it requires every number to be stated, never a half-default mix.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "custom_components/roborock_plus/garage_config.py"


@pytest.fixture
def config():
    spec = spec_from_file_location("garage_config_controlled_exit", PATH)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def enabled_binding(config, **overrides):
    binding = {
        "enabled": True,
        "task_door_mode": "after_exit",
        "cover_entity_id": "cover.garage",
        "open_source": "contact",
        "close_source": "contact",
        "open_contact": "binary_sensor.open",
        "open_contact_state": "on",
        "close_contact": "binary_sensor.closed",
        "close_contact_state": "on",
        "contact_settle_seconds": 1,
        "dock_settle_seconds": 5,
        "operation_timeout_seconds": 90,
        "handover_confirmed": True,
        "controlled_exit_enabled": True,
        "controlled_exit_x": 25696,
        "controlled_exit_y": 26500,
        "controlled_exit_timeout_seconds": 120,
    }
    binding.update(overrides)
    return binding


# --- default off -------------------------------------------------------------

def test_the_feature_is_absent_unless_declared(config):
    """An existing 0.8.1 binding must validate and gain nothing."""
    binding = enabled_binding(config)
    for key in ("controlled_exit_enabled", *config.CONTROLLED_EXIT_FIELDS):
        binding.pop(key)
    result = config.validate_binding(binding)
    assert "controlled_exit_enabled" not in result
    assert not any(key in result for key in config.CONTROLLED_EXIT_FIELDS)


def test_explicitly_disabled_keeps_the_numbers_for_later(config):
    binding = enabled_binding(config, controlled_exit_enabled=False)
    result = config.validate_binding(binding)
    assert result["controlled_exit_enabled"] is False
    assert result["controlled_exit_x"] == 25696


def test_a_disabled_draft_may_omit_the_numbers(config):
    binding = enabled_binding(config, controlled_exit_enabled=False)
    for key in config.CONTROLLED_EXIT_FIELDS:
        binding.pop(key)
    result = config.validate_binding(binding)
    assert result["controlled_exit_enabled"] is False


# --- enabling requires the whole target --------------------------------------

@pytest.mark.parametrize("field", [
    "controlled_exit_x", "controlled_exit_y", "controlled_exit_timeout_seconds",
])
def test_enabling_without_a_field_is_refused(config, field):
    binding = enabled_binding(config)
    binding.pop(field)
    with pytest.raises(config.GarageConfigError) as err:
        config.validate_binding(binding)
    assert err.value.reason == "missing_controlled_exit_target"
    assert err.value.field == field


def test_enabling_with_every_field_is_accepted(config):
    result = config.validate_binding(enabled_binding(config))
    assert result["controlled_exit_enabled"] is True
    assert result["controlled_exit_x"] == 25696
    assert result["controlled_exit_y"] == 26500


# --- shape checks ------------------------------------------------------------

def test_the_switch_must_be_a_boolean(config):
    with pytest.raises(config.GarageConfigError) as err:
        config.validate_binding(enabled_binding(config, controlled_exit_enabled="yes"))
    assert err.value.reason == "invalid_boolean_value"


@pytest.mark.parametrize("field,value", [
    ("controlled_exit_x", -1),
    ("controlled_exit_y", 65536),
    ("controlled_exit_timeout_seconds", 9),
    ("controlled_exit_timeout_seconds", 301),
])
def test_out_of_range_values_are_refused(config, field, value):
    with pytest.raises(config.GarageConfigError):
        config.validate_binding(enabled_binding(config, **{field: value}))


def test_a_zero_waypoint_is_legal_as_a_coordinate(config):
    """0 is a valid map coordinate; refusing it would invent a rule. The
    run-time arrival check is what stops a nonsense target from succeeding."""
    result = config.validate_binding(
        enabled_binding(config, controlled_exit_x=0, controlled_exit_y=0))
    assert result["controlled_exit_x"] == 0


def test_the_disabled_path_adds_no_keys(config):
    binding = enabled_binding(config)
    for key in ("controlled_exit_enabled", *config.CONTROLLED_EXIT_FIELDS):
        binding.pop(key)
    result = config.validate_binding(binding)
    assert set(result) == set(binding)


def test_the_defaults_describe_the_measured_geometry(config):
    """The shipped defaults must match the numbers the spec reasons about."""
    assert config.DEFAULT_CONTROLLED_EXIT_X == 25696
    assert config.DEFAULT_CONTROLLED_EXIT_Y == 26500
    assert config.CONTROLLED_EXIT_TOLERANCE_MM == 400
