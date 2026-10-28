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
    "controlled_exit_x": (0, 65535),
    "controlled_exit_y": (0, 65535),
    "controlled_exit_timeout_seconds": (10, 300),
}
EVIDENCE_SOURCES = ("contact", "position", "timed")

# Controlled exit: drive the robot straight out of the cabinet before a clean
# command, close the door at the waypoint, then start cleaning.
#
# The default waypoint is this household's measured geometry: the robot parks at
# (25696, 28439) and the danger zone's lower edge is y=27123, so y=26500 is
# 747mm past that edge -- enough to rotate, not so far that the trip looks like a
# detour. It is only a form default; enabling the feature requires the operator
# to state all three numbers explicitly, because default-x-with-custom-y is a
# combination nobody has verified.
DEFAULT_CONTROLLED_EXIT_X = 25696
DEFAULT_CONTROLLED_EXIT_Y = 26500
DEFAULT_CONTROLLED_EXIT_TIMEOUT = 120

# How close the reported position must be to the waypoint to count as arrived.
# Robot docking precision is not zero; demanding the exact point would time out
# forever. 400mm is smaller than the body, so the robot really is "there".
CONTROLLED_EXIT_TOLERANCE_MM = 400

CONTROLLED_EXIT_FIELDS = (
    "controlled_exit_x",
    "controlled_exit_y",
    "controlled_exit_timeout_seconds",
)


def required_timing_fields(binding: Mapping[str, Any]) -> tuple[str, ...]:
    """Return only timings consumed by the explicitly selected evidence."""
    required = {"operation_timeout_seconds", "dock_settle_seconds"}
    for direction in ("open", "close"):
        source = binding.get(f"{direction}_source")
        if source == "contact":
            required.add("contact_settle_seconds")
        if source == "position":
            required.add("position_settle_seconds")
        if source == "timed" or (source == "position" and binding.get("position_kind") == "estimated"):
            required.update((f"{direction}_travel_seconds", "travel_margin_seconds"))
    return tuple(key for key in NUMERIC_RANGES if key in required)


def suggested_timing_values(binding: Mapping[str, Any]) -> dict[str, int]:
    """Suggest UI policy values; never invent hardware travel or repair a binding."""
    required = required_timing_fields(binding)
    suggestions = {key: value for key, value in {
        # A magnetic door contact is a latched binary switch: one coherent reading is the
        # fact. A settle window only filters chatter, and chatter can only be observed by
        # polling -- this integration has no polling loop, so a non-zero window never
        # elapses and every endpoint stays "unsettled" forever. That made a closed door
        # unreadable and the whole binding inert. Zero is the correct default for a
        # contact; a position value genuinely varies while the door travels, so it keeps
        # its window.
        "travel_margin_seconds": 10, "contact_settle_seconds": 0,
        "position_settle_seconds": 2, "dock_settle_seconds": 5,
    }.items() if key in required}
    values = {**suggestions, **binding}
    longest = 0.0
    for direction in ("open", "close"):
        try:
            longest = max(longest, _endpoint_wait(values, direction))
        except GarageConfigError:
            # Keep the other known direction's wait. The incomplete direction
            # remains a required input and cannot pass strict validation.
            continue
    suggestions["operation_timeout_seconds"] = max(90, math.ceil(longest + 30))
    return suggestions


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


def _endpoint_wait(binding: Mapping[str, Any], direction: str) -> float:
    source = binding.get(f"{direction}_source")
    if source not in EVIDENCE_SOURCES:
        raise GarageConfigError("invalid_evidence_source", f"{direction}_source")
    wait = 0.0
    if source in ("contact", "position"):
        key = "contact_settle_seconds" if source == "contact" else "position_settle_seconds"
        wait += _number(binding, key, *NUMERIC_RANGES[key])
    if source == "timed" or (source == "position" and binding.get("position_kind") == "estimated"):
        for key in (f"{direction}_travel_seconds", "travel_margin_seconds"):
            wait += _number(binding, key, *NUMERIC_RANGES[key])
    return wait


def longest_endpoint_wait(binding: Mapping[str, Any]) -> float:
    """Strictly require both selected-source waits for validation and error hints."""
    return max(_endpoint_wait(binding, "open"), _endpoint_wait(binding, "close"))


def _entity(value: Any, domains: tuple[str, ...], field: str) -> None:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", value)
        or value.split(".", 1)[0] not in domains
    ):
        raise GarageConfigError("invalid_entity_reference", field)


def _validate_supplied_fields(binding: Mapping[str, Any]) -> None:
    """Validate supplied values independently of whether a draft is complete."""
    for key in ("enabled", "allow_estimated", "handover_confirmed", "controlled_exit_enabled"):
        if key in binding and not isinstance(binding[key], bool):
            raise GarageConfigError("invalid_boolean_value", key)
    for key, (minimum, maximum) in {**NUMERIC_RANGES, "open_position": (0, 100), "close_position": (0, 100)}.items():
        if key in binding:
            _number(binding, key, minimum, maximum)
    if "open_position" in binding and "close_position" in binding and binding["close_position"] >= binding["open_position"]:
        raise GarageConfigError("invalid_position_thresholds", "open_position")
    for key, domains in (
        ("cover_entity_id", ("cover",)),
        ("close_permission_entity", ("input_boolean", "binary_sensor")),
        ("open_contact", ("binary_sensor",)),
        ("close_contact", ("binary_sensor",)),
    ):
        if binding.get(key) not in (None, ""):
            _entity(binding[key], domains, key)
    for key, choices, reason in (
        ("task_door_mode", ("hold_open", "after_exit"), "invalid_task_door_mode"),
        ("open_source", EVIDENCE_SOURCES, "invalid_evidence_source"),
        ("close_source", EVIDENCE_SOURCES, "invalid_evidence_source"),
        ("open_contact_state", ("on", "off"), "invalid_contact_configuration"),
        ("close_contact_state", ("on", "off"), "invalid_contact_configuration"),
        ("position_kind", ("measured", "estimated"), "invalid_position_configuration"),
    ):
        value = binding.get(key)
        if value not in (None, "") and value not in choices:
            raise GarageConfigError(reason, key)
    if binding.get("open_contact") and binding.get("open_contact") == binding.get("close_contact"):
        raise GarageConfigError("duplicate_endpoint", "close_contact")


def validate_binding(binding: Mapping[str, Any], *, supported: bool = True) -> dict[str, Any]:
    """Validate enabled settings without coercion or optimistic safety defaults.

    Incomplete disabled drafts can be stored. Validation concerns configuration,
    so an offline source can be saved but cannot create runtime permission.
    """
    if not isinstance(binding, Mapping):
        raise GarageConfigError("invalid_binding")
    result = dict(binding)
    result.setdefault("enabled", False)
    _validate_supplied_fields(result)
    if not result["enabled"]:
        return result
    if not supported:
        raise GarageConfigError("unsupported_robot")
    result.setdefault("task_door_mode", "hold_open")
    if result["task_door_mode"] not in ("hold_open", "after_exit"):
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
    for key in required_timing_fields(result):
        _number(result, key, *NUMERIC_RANGES[key])
    longest_wait = longest_endpoint_wait(result)
    if result["operation_timeout_seconds"] <= longest_wait:
        raise GarageConfigError("operation_timeout_too_short", "operation_timeout_seconds")
    permission = result.get("close_permission_entity")
    if permission is not None and permission != "":
        _entity(permission, ("input_boolean", "binary_sensor"), "close_permission_entity")
    if not result.get("handover_confirmed", False):
        raise GarageConfigError("handover_required", "handover_confirmed")
    if result.get("controlled_exit_enabled", False):
        # Enabling requires all three numbers stated by the operator. A default
        # x paired with a custom y is a geometry nobody measured.
        for key in CONTROLLED_EXIT_FIELDS:
            if key not in result:
                raise GarageConfigError("missing_controlled_exit_target", key)
            _number(result, key, *NUMERIC_RANGES[key])
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
    """Reject a cover already claimed by an enabled binding in another entry.

    The flat global guard used to be a second owner to check against. It is gone,
    so ownership is now decided purely between native bindings.
    """
    if binding.get("enabled") is not True:
        return
    cover = binding.get("cover_entity_id")
    for other_entry_id, options, _v1_duids in entries:
        others = options.get(CONF_GARAGE_BINDINGS, {})
        if not isinstance(others, Mapping):
            raise GarageConfigError("invalid_binding")
        for other_duid, other in others.items():
            if other_entry_id == entry_id and other_duid == duid:
                continue
            if (isinstance(other, Mapping) and other.get("enabled") is True
                    and other.get("cover_entity_id") == cover):
                raise GarageConfigError("duplicate_binding", "cover_entity_id")


def validate_legacy_guard_ownership(
    options: Mapping[str, Any],
    *,
    entry_id: str,
    entries: Iterable[tuple[str, Mapping[str, Any], Iterable[str]]],
) -> None:
    """Retired: the flat guard this protected no longer exists.

    Kept as a no-op so callers written against the old API keep importing, and so
    the removal is visible here rather than as a silent disappearance. Ownership
    between native bindings is enforced by ``validate_binding_ownership``.
    """
