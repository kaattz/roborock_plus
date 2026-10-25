"""Strict, HA-independent safety configuration checks."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "custom_components/roborock_plus/garage_config.py"


@pytest.fixture
def config():
    assert PATH.is_file(), "The garage configuration validator has not been implemented"
    spec = spec_from_file_location("garage_config_under_test", PATH)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def binding(**changes):
    result = {
        "enabled": True,
        "cover_entity_id": "cover.garage",
        "task_door_mode": "hold_open",
        "open_source": "timed",
        "close_source": "timed",
        "open_travel_seconds": 45,
        "close_travel_seconds": 40,
        "travel_margin_seconds": 2,
        "contact_settle_seconds": 1,
        "position_settle_seconds": 1,
        "operation_timeout_seconds": 90,
        "dock_settle_seconds": 5,
        "allow_estimated": True,
        "handover_confirmed": True,
    }
    result.update(changes)
    return result


def test_enabled_binding_is_validated_without_mutating_input(config):
    value = binding()
    resolved = config.validate_binding(value)
    assert resolved == value
    assert resolved is not value


def test_disabled_draft_is_not_implicitly_enabled_or_migrated(config):
    assert config.validate_binding({})["enabled"] is False
    assert config.resolve_binding({"garage_guard_enabled": True, "garage_door_entity_id": "cover.old"}, "v1") is None
    assert config.resolve_binding({"garage_bindings": {"v1": {"enabled": False}}}, "v1") is None


@pytest.mark.parametrize("key", [
    "open_travel_seconds", "close_travel_seconds", "travel_margin_seconds",
    "contact_settle_seconds", "position_settle_seconds", "operation_timeout_seconds",
    "dock_settle_seconds",
])
@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), "45", None])
def test_numeric_fields_reject_nonfinite_boolean_and_coerced_values(config, key, value):
    with pytest.raises(config.GarageConfigError) as error:
        config.validate_binding(binding(**{key: value}))
    assert error.value.reason == "invalid_numeric_value"


@pytest.mark.parametrize("key,value", [
    ("open_travel_seconds", 0), ("close_travel_seconds", 601),
    ("travel_margin_seconds", -1), ("travel_margin_seconds", 121),
    ("position_settle_seconds", 61), ("contact_settle_seconds", -1),
    ("dock_settle_seconds", 0), ("dock_settle_seconds", 301),
    ("operation_timeout_seconds", 1801),
])
def test_numeric_ranges_are_not_clamped(config, key, value):
    with pytest.raises(config.GarageConfigError, match="invalid_numeric_value"):
        config.validate_binding(binding(**{key: value}))


def test_extremely_large_numbers_report_configuration_error(config):
    with pytest.raises(config.GarageConfigError, match="invalid_numeric_value"):
        config.validate_binding(binding(open_travel_seconds=10 ** 1000))


@pytest.mark.parametrize("key", [
    "open_travel_seconds", "close_travel_seconds", "travel_margin_seconds",
    "operation_timeout_seconds", "dock_settle_seconds",
])
def test_new_binding_requires_explicit_timing(config, key):
    value = binding()
    del value[key]
    with pytest.raises(config.GarageConfigError, match="missing_safety_configuration"):
        config.validate_binding(value)


def test_sources_are_explicit_independent_and_never_guessed(config):
    value = binding()
    del value["open_source"]
    with pytest.raises(config.GarageConfigError, match="missing_safety_configuration"):
        config.validate_binding(value)
    with pytest.raises(config.GarageConfigError, match="invalid_evidence_source"):
        config.validate_binding(binding(open_source="auto"))
    mixed = binding(open_source="contact", open_contact="binary_sensor.open", open_contact_state="off")
    assert config.validate_binding(mixed)["open_contact_state"] == "off"


def test_contacts_require_explicit_polarity_and_distinct_entities(config):
    with pytest.raises(config.GarageConfigError, match="invalid_contact_configuration"):
        config.validate_binding(binding(open_source="contact", open_contact="binary_sensor.end"))
    with pytest.raises(config.GarageConfigError, match="invalid_contact_configuration"):
        config.validate_binding(binding(open_source="contact", open_contact="binary_sensor.end", open_contact_state="open"))
    with pytest.raises(config.GarageConfigError, match="duplicate_endpoint"):
        config.validate_binding(binding(open_source="contact", close_source="contact", open_contact="binary_sensor.end", close_contact="binary_sensor.end", open_contact_state="on", close_contact_state="off"))


@pytest.mark.parametrize("close,open_", [(5, 5), (95, 5), (-1, 95), (5, 101), (False, 95), (5, float("nan"))])
def test_measured_position_thresholds_are_validated(config, close, open_):
    with pytest.raises(config.GarageConfigError):
        config.validate_binding(binding(open_source="position", position_kind="measured", close_position=close, open_position=open_))


def test_measured_and_estimated_positions_are_explicit(config):
    with pytest.raises(config.GarageConfigError, match="invalid_position_configuration"):
        config.validate_binding(binding(open_source="position", open_position=95, close_position=5))
    value = binding(open_source="position", close_source="position", position_kind="measured", open_position=95, close_position=5, allow_estimated=False)
    assert config.validate_binding(value)["position_kind"] == "measured"
    with pytest.raises(config.GarageConfigError, match="estimated_risk_not_accepted"):
        config.validate_binding(binding(allow_estimated=False))


@pytest.mark.parametrize("timeout", [1, 47])
def test_timeout_must_exceed_travel_margin_and_settle(config, timeout):
    with pytest.raises(config.GarageConfigError, match="operation_timeout_too_short"):
        config.validate_binding(binding(operation_timeout_seconds=timeout))


def test_handover_and_real_v1_capabilities_cannot_be_bypassed(config):
    with pytest.raises(config.GarageConfigError, match="handover_required"):
        config.validate_binding(binding(handover_confirmed=False))
    assert config.validate_binding(binding(task_door_mode="after_exit"))["task_door_mode"] == "after_exit"
    with pytest.raises(config.GarageConfigError, match="unsupported_robot"):
        config.validate_binding(binding(), supported=False)
    for key in ("enabled", "allow_estimated", "handover_confirmed"):
        with pytest.raises(config.GarageConfigError, match="invalid_boolean_value"):
            config.validate_binding(binding(**{key: "true"}))


@pytest.mark.parametrize("mode", ["hold_open", "after_exit"])
def test_both_modes_keep_explicit_handover_sources_estimation_and_robot_guards(config, mode):
    value = binding(task_door_mode=mode)
    assert config.validate_binding(value)["task_door_mode"] == mode
    with pytest.raises(config.GarageConfigError, match="handover_required"):
        config.validate_binding({**value, "handover_confirmed": False})
    with pytest.raises(config.GarageConfigError, match="estimated_risk_not_accepted"):
        config.validate_binding({**value, "allow_estimated": False})
    with pytest.raises(config.GarageConfigError, match="invalid_evidence_source"):
        config.validate_binding({**value, "open_source": "auto"})
    with pytest.raises(config.GarageConfigError, match="unsupported_robot"):
        config.validate_binding(value, supported=False)
    with pytest.raises(config.GarageConfigError, match="invalid_task_door_mode"):
        config.validate_binding({**value, "task_door_mode": "open_on_return_without_pause"})


def test_mode_defaults_remain_disabled_and_hold_open(config):
    assert config.validate_binding({})["enabled"] is False
    value = binding()
    value.pop("task_door_mode")
    assert config.validate_binding(value)["task_door_mode"] == "hold_open"


def test_contact_binding_does_not_require_unused_position_or_estimate_timings(config):
    value = binding(open_source="contact", close_source="contact", open_contact="binary_sensor.open", close_contact="binary_sensor.closed", open_contact_state="on", close_contact_state="off", allow_estimated=False)
    for key in ("open_travel_seconds", "close_travel_seconds", "travel_margin_seconds", "position_settle_seconds"):
        value.pop(key)
    assert config.validate_binding(value) == value
    value.pop("contact_settle_seconds")
    with pytest.raises(config.GarageConfigError, match="missing_safety_configuration"):
        config.validate_binding(value)


def test_measured_position_binding_only_requires_its_selected_feedback_timing(config):
    value = binding(open_source="position", close_source="position", position_kind="measured", open_position=95, close_position=5, allow_estimated=False)
    for key in ("open_travel_seconds", "close_travel_seconds", "travel_margin_seconds", "contact_settle_seconds"):
        value.pop(key)
    assert config.validate_binding(value) == value
    value.pop("position_settle_seconds")
    with pytest.raises(config.GarageConfigError, match="missing_safety_configuration"):
        config.validate_binding(value)


def test_estimated_position_keeps_full_travel_margin_and_position_stabilization(config):
    value = binding(open_source="position", close_source="position", position_kind="estimated", open_position=95, close_position=5)
    value.pop("contact_settle_seconds")
    assert config.validate_binding(value) == value
    for key in ("open_travel_seconds", "close_travel_seconds", "travel_margin_seconds", "position_settle_seconds"):
        missing = dict(value)
        missing.pop(key)
        with pytest.raises(config.GarageConfigError, match="missing_safety_configuration"):
            config.validate_binding(missing)


def test_unused_but_retained_values_are_still_strictly_checked(config):
    value = binding(contact_settle_seconds=float("nan"))
    with pytest.raises(config.GarageConfigError, match="invalid_numeric_value"):
        config.validate_binding(value)
    with pytest.raises(config.GarageConfigError, match="invalid_numeric_value"):
        config.validate_binding({**value, "enabled": False})


@pytest.mark.parametrize("field,value,reason", [
    ("cover_entity_id", "switch.door", "invalid_entity_reference"),
    ("cover_entity_id", "cover.invalid space", "invalid_entity_reference"),
    ("cover_entity_id", {}, "invalid_entity_reference"),
    ("close_permission_entity", False, "invalid_entity_reference"),
    ("close_permission_entity", "sensor.permission", "invalid_entity_reference"),
    ("open_source", "auto", "invalid_evidence_source"),
    ("close_source", [], "invalid_evidence_source"),
    ("task_door_mode", "open_without_pause", "invalid_task_door_mode"),
    ("task_door_mode", {}, "invalid_task_door_mode"),
    ("open_contact", "switch.endpoint", "invalid_entity_reference"),
    ("close_contact_state", {}, "invalid_contact_configuration"),
    ("position_kind", [], "invalid_position_configuration"),
])
def test_draft_allows_missing_fields_but_rejects_every_supplied_illegal_reference_or_enum(config, field, value, reason):
    with pytest.raises(config.GarageConfigError) as error:
        config.validate_binding({"enabled": False, field: value})
    assert error.value.reason == reason
    assert error.value.field == field


def test_draft_preserves_empty_optional_and_inactive_fields_without_guessing(config):
    draft = {"enabled": False, "cover_entity_id": "", "close_permission_entity": "",
             "open_source": "", "close_source": None, "task_door_mode": "",
             "open_contact": "", "close_contact_state": None, "position_kind": ""}
    assert config.validate_binding(draft) == draft


def test_draft_cannot_store_two_identical_endpoint_references(config):
    with pytest.raises(config.GarageConfigError, match="duplicate_endpoint"):
        config.validate_binding({"enabled": False, "open_contact": "binary_sensor.same", "close_contact": "binary_sensor.same"})


def test_entity_domains_and_permission_are_checked(config):
    for value in (binding(cover_entity_id="switch.door"), binding(close_permission_entity="sensor.occupancy"), binding(close_permission_entity=False)):
        with pytest.raises(config.GarageConfigError, match="invalid_entity_reference"):
            config.validate_binding(value)
    assert config.validate_binding(binding(close_permission_entity="input_boolean.allow"))["close_permission_entity"] == "input_boolean.allow"


def test_resolve_enabled_errors_are_visible(config):
    with pytest.raises(config.GarageConfigError, match="missing_safety_configuration"):
        config.resolve_binding({"garage_control_version": 1, "garage_bindings": {"v1": {"enabled": True}}}, "v1")
    with pytest.raises(config.GarageConfigError, match="unsupported_configuration_version"):
        config.resolve_binding({"garage_control_version": 2, "garage_bindings": {"v1": binding()}}, "v1")
    options = {"garage_control_version": 1, "garage_bindings": {"v1": binding()}}
    assert config.resolve_binding(options, "v1")["cover_entity_id"] == "cover.garage"


def test_ownership_rejects_a_door_another_enabled_binding_already_holds(config):
    """Ownership is now decided purely between native bindings.

    The flat guard used to be a second owner to check against; with it gone the
    only conflict that can exist is two enabled bindings claiming one cover.
    """
    entries = [("entry", {"garage_bindings": {"other": binding()}}, ("v1", "other"))]
    with pytest.raises(config.GarageConfigError, match="duplicate_binding"):
        config.validate_binding_ownership(binding(), duid="v1", entry_id="entry", entries=entries)


def test_ownership_ignores_disabled_bindings_in_other_entries(config):
    entries = [("entry", {"garage_bindings": {"other": binding(enabled=False)}}, ("v1", "other"))]
    config.validate_binding_ownership(binding(), duid="v1", entry_id="entry", entries=entries)


def test_ownership_allows_a_robot_holding_its_own_door(config):
    entries = [("entry", {"garage_bindings": {"v1": binding()}}, ("v1",))]
    config.validate_binding_ownership(binding(), duid="v1", entry_id="entry", entries=entries)
    config.validate_binding_ownership({"enabled": False}, duid="v1", entry_id="entry", entries=entries)


def test_the_retired_legacy_ownership_check_is_a_no_op(config):
    """The flat guard is gone, so its ownership check must not reject anything."""
    legacy = {"garage_guard_enabled": True, "garage_door_entity_id": "cover.garage"}
    config.validate_legacy_guard_ownership(legacy, entry_id="entry", entries=[("entry", legacy, ("v1", "other"))])
    owner = ("new", {"garage_bindings": {"third": binding()}}, ("third",))
    config.validate_legacy_guard_ownership(
        legacy, entry_id="entry", entries=[("entry", legacy, ("v1", "other")), owner])


@pytest.mark.parametrize("version", [True, "1", 1.0, None])
def test_configuration_version_is_an_integer_not_a_coercion(config, version):
    with pytest.raises(config.GarageConfigError, match="unsupported_configuration_version"):
        config.resolve_binding({"garage_control_version": version, "garage_bindings": {"v1": binding()}}, "v1")

