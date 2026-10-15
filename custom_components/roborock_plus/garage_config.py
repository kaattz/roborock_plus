"""Pure validation of per-robot garage bindings; never operates a device."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math
import re
from typing import Any

CONF_GARAGE_BINDINGS = "garage_bindings"
CONF_GARAGE_CONTROL_VERSION = "garage_control_version"
GARAGE_CONTROL_VERSION = 1

NUMERIC_RANGES = {
    "open_travel_seconds": (1, 600),
    "close_travel_seconds": (1, 600),
    "travel_margin_seconds": (0, 120),
    "contact_settle_seconds": (0, 60),
    "position_settle_seconds": (0, 60),
    "operation_timeout_seconds": (1, 1800),
    "dock_settle_seconds": (1, 300),
}
EVIDENCE_SOURCES = ("contact", "position", "timed")


class GarageConfigError(ValueError):
    """A stable machine-readable configuration failure."""

    def __init__(self, reason: str, field: str | None = None) -> None:
        self.reason = reason
        self.field = field
        super().__init__(reason if field is None else f"{reason}: {field}")


def _number(binding: Mapping[str, Any], key: str, minimum: float, maximum: float) -> float:
    if key not in binding:
        raise GarageConfigError("missing_safety_configuration", key)
    value = binding[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= value <= maximum
        or not math.isfinite(value)
    ):
        raise GarageConfigError("invalid_numeric_value", key)
    return value


def _entity(value: Any, domains: tuple[str, ...], field: str) -> None:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", value)
        or value.split(".", 1)[0] not in domains
    ):
        raise GarageConfigError("invalid_entity_reference", field)


def validate_binding(binding: Mapping[str, Any], *, supported: bool = True) -> dict[str, Any]:
    """Validate enabled settings without coercion or optimistic safety defaults.

    Incomplete disabled drafts can be stored. Validation concerns configuration,
    so an offline source can be saved but cannot create runtime permission.
    """
    if not isinstance(binding, Mapping):
        raise GarageConfigError("invalid_binding")
    result = dict(binding)
    result.setdefault("enabled", False)
    for key in ("enabled", "allow_estimated", "handover_confirmed"):
        if key in result and not isinstance(result[key], bool):
            raise GarageConfigError("invalid_boolean_value", key)
    if not result["enabled"]:
        return result
    if not supported:
        raise GarageConfigError("unsupported_robot")
    result.setdefault("task_door_mode", "hold_open")
    if result["task_door_mode"] == "after_exit":
        raise GarageConfigError("unsupported_return_interlock", "task_door_mode")
    if result["task_door_mode"] != "hold_open":
        raise GarageConfigError("invalid_task_door_mode", "task_door_mode")
    if "cover_entity_id" not in result:
        raise GarageConfigError("missing_safety_configuration", "cover_entity_id")
    _entity(result["cover_entity_id"], ("cover",), "cover_entity_id")
    for direction in ("open", "close"):
        source_key = f"{direction}_source"
        if source_key not in result:
            raise GarageConfigError("missing_safety_configuration", source_key)
        if result[source_key] not in EVIDENCE_SOURCES:
            raise GarageConfigError("invalid_evidence_source", source_key)
        if result[source_key] == "contact":
            contact_key = f"{direction}_contact"
            if not result.get(contact_key) or result.get(f"{direction}_contact_state") not in ("on", "off"):
                raise GarageConfigError("invalid_contact_configuration", contact_key)
            _entity(result[contact_key], ("binary_sensor",), contact_key)
    if result.get("open_contact") and result.get("open_contact") == result.get("close_contact"):
        raise GarageConfigError("duplicate_endpoint", "close_contact")
    uses_position = "position" in (result["open_source"], result["close_source"])
    if uses_position:
        if result.get("position_kind") not in ("measured", "estimated"):
            raise GarageConfigError("invalid_position_configuration", "position_kind")
        close = _number(result, "close_position", 0, 100)
        open_ = _number(result, "open_position", 0, 100)
        if close >= open_:
            raise GarageConfigError("invalid_position_thresholds", "open_position")
    uses_estimate = "timed" in (result["open_source"], result["close_source"]) or (
        uses_position and result["position_kind"] == "estimated"
    )
    if uses_estimate and not result.get("allow_estimated", False):
        raise GarageConfigError("estimated_risk_not_accepted", "allow_estimated")
    for key, (minimum, maximum) in NUMERIC_RANGES.items():
        _number(result, key, minimum, maximum)
    longest_wait = max(result["open_travel_seconds"], result["close_travel_seconds"]) + result["travel_margin_seconds"] + max(result["contact_settle_seconds"], result["position_settle_seconds"])
    if result["operation_timeout_seconds"] <= longest_wait:
        raise GarageConfigError("operation_timeout_too_short", "operation_timeout_seconds")
    permission = result.get("close_permission_entity")
    if permission is not None and permission != "":
        _entity(permission, ("input_boolean", "binary_sensor"), "close_permission_entity")
    if not result.get("handover_confirmed", False):
        raise GarageConfigError("handover_required", "handover_confirmed")
    return result


def resolve_binding(options: Mapping[str, Any], duid: str) -> dict[str, Any] | None:
    """Return an explicitly enabled valid binding; never migrate the old guard."""
    bindings = options.get(CONF_GARAGE_BINDINGS, {})
    if not isinstance(bindings, Mapping):
        raise GarageConfigError("invalid_binding")
    binding = bindings.get(duid)
    if binding is None:
        return None
    validated = validate_binding(binding)
    if not validated["enabled"]:
        return None
    version = options.get(CONF_GARAGE_CONTROL_VERSION)
    if type(version) is not int or version != GARAGE_CONTROL_VERSION:
        raise GarageConfigError("unsupported_configuration_version")
    return validated


def validate_binding_ownership(
    binding: Mapping[str, Any],
    *,
    duid: str,
    entry_id: str,
    entries: Iterable[tuple[str, Mapping[str, Any], Iterable[str]]],
) -> None:
    """Reject another owner or an unresolved robot using the old global guard.

    ``entries`` contains (entry_id, options, V1 duids). An unloaded entry with a
    matching old guard and no known duids is an unresolved conflict as well.
    The runtime must repeat this check; the UI is not an ownership registry.
    """
    if binding.get("enabled") is not True:
        return
    cover = binding.get("cover_entity_id")
    for other_entry_id, options, v1_duids in entries:
        others = options.get(CONF_GARAGE_BINDINGS, {})
        if not isinstance(others, Mapping):
            raise GarageConfigError("invalid_binding")
        for other_duid, other in others.items():
            if other_entry_id == entry_id and other_duid == duid:
                continue
            if isinstance(other, Mapping) and other.get("enabled") is True and other.get("cover_entity_id") == cover:
                raise GarageConfigError("duplicate_binding", "cover_entity_id")
        if options.get("garage_guard_enabled") is not True or options.get("garage_door_entity_id") != cover:
            continue
        known_duids = tuple(v1_duids)
        if not known_duids:
            raise GarageConfigError("legacy_guard_conflict", "cover_entity_id")
        for other_duid in known_duids:
            if other_entry_id == entry_id and other_duid == duid:
                continue
            other = others.get(other_duid)
            if not isinstance(other, Mapping) or other.get("enabled") is not True:
                raise GarageConfigError("legacy_guard_conflict", "cover_entity_id")


def validate_legacy_guard_ownership(
    options: Mapping[str, Any],
    *,
    entry_id: str,
    entries: Iterable[tuple[str, Mapping[str, Any], Iterable[str]]],
) -> None:
    """Prevent an old-guard edit from taking a cover already managed natively.

    Unmigrated multi-robot global guards remain compatible when the cover has
    no native owner. Robots with enabled native bindings forward directly, so
    they are not legacy users of a leftover global option.
    """
    cover = options.get("garage_door_entity_id")
    if options.get("garage_guard_enabled") is not True or not cover:
        return
    entries = tuple(entries)
    own = options.get(CONF_GARAGE_BINDINGS, {})
    if not isinstance(own, Mapping):
        raise GarageConfigError("invalid_binding")
    duids = next((tuple(duids) for other_entry_id, _, duids in entries if other_entry_id == entry_id), ())
    if duids and all(
        isinstance(own.get(duid), Mapping) and own[duid].get("enabled") is True
        for duid in duids
    ):
        return
    for _, other_options, _ in entries:
        others = other_options.get(CONF_GARAGE_BINDINGS, {})
        if not isinstance(others, Mapping):
            raise GarageConfigError("invalid_binding")
        if any(
            isinstance(binding, Mapping)
            and binding.get("enabled") is True
            and binding.get("cover_entity_id") == cover
            for binding in others.values()
        ):
            raise GarageConfigError("legacy_guard_conflict", "cover_entity_id")
