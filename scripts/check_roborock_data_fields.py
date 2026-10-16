"""Verify the library data fields this component reads still exist.

The attribute checker resolves chains that start from an imported name, for
example ``RoborockDockTypeCode.o0_dock``. It cannot see fields reached through
a device object, such as ``state.status.dock_type`` or
``data.consumable.filter_time_left``, because those names are never imported.

This walks the literals in the component source and checks the first attribute
after each group against the trait classes in the installed python-roborock.
Only the first level is checked: text alone cannot tell ``.status.battery`` (a
trait accessor) from ``.status.value`` (the enum behind the Q10 ``status``
field), so deeper reads are left to the runtime checks in
``smoke_import_component.py``.

    .analysis/venv712/Scripts/python.exe scripts/check_roborock_data_fields.py
"""

from __future__ import annotations

import enum
import importlib
import re
import sys
import types
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "roborock_plus"

# field-group prefix in the component source -> trait class that provides it.
GROUPS = {
    "status": "roborock.devices.traits.v1.status:StatusTrait",
    "consumable": "roborock.devices.traits.v1.consumeable:ConsumableTrait",
    "clean_summary": "roborock.devices.traits.v1.clean_summary:CleanSummaryTrait",
}

# ``.status.X`` is ambiguous: V1 files read the V1 status trait, while the Q10
# platform files read the Q10 status trait. Both are accepted for ``status``.
Q10_STATUS = "roborock.devices.traits.b01.q10.status:StatusTrait"

# ``data.status.value`` parses two ways at text level. Either ``status`` is the
# trait and ``value`` an attribute of it, or ``status`` is a *field* of the Q10
# status container (typed ``YXDeviceState``) and ``value`` is read on that enum
# member. The second parse is checked against this container.
Q10_CONTAINER = "roborock.data.b01_q10.b01_q10_containers:Q10Status"

# Accessors and methods that belong to the trait but are not data fields.
ALLOWED_NON_FIELDS = {
    "refresh",
    "add_update_listener",
    "add_listener",
    "update_from_dps",
    "resolve_error",
    "set_cleaning_mode",
}


def load_class(dotted: str) -> type:
    module_name, _, class_name = dotted.partition(":")
    return getattr(importlib.import_module(module_name), class_name)


def trait_fields(dotted: str) -> set[str]:
    """Every attribute name a trait class can expose, across its MRO."""
    names: set[str] = set()
    for klass in load_class(dotted).__mro__:
        names.update(getattr(klass, "__annotations__", {}) or {})
        names.update(vars(klass))
    return {n for n in names if not n.startswith("__")}


def field_type(dotted: str, name: str) -> type | None:
    """Declared type of field *name* on a class, or None when unresolvable."""
    for klass in load_class(dotted).__mro__:
        try:
            hints = typing.get_type_hints(klass)
        except Exception:  # noqa: BLE001 - unresolved forward refs are fine
            hints = {}
        declared = hints.get(name)
        if declared is None:
            continue
        # ``X | None`` is types.UnionType on 3.10+; Optional[X] is typing.Union.
        if (
            isinstance(declared, types.UnionType)
            or typing.get_origin(declared) is typing.Union
        ):
            args = [a for a in typing.get_args(declared) if a is not type(None)]
            declared = args[0] if args else None
        if isinstance(declared, type):
            return declared
    return None


def exposes(resolved: type, child: str) -> bool:
    """Whether *child* is readable on a value of type *resolved*.

    Enum members carry ``.value``/``.name`` even though the enum class itself
    does not, so enum types are checked through one of their members.
    """
    if hasattr(resolved, child):
        return True
    if isinstance(resolved, type) and issubclass(resolved, enum.Enum):
        return any(hasattr(member, child) for member in resolved)
    return False


def main() -> int:
    from importlib.metadata import version

    try:
        pkg = version("python-roborock")
    except Exception:  # noqa: BLE001
        pkg = "unknown"

    print(f"python-roborock: {pkg}")

    available: dict[str, set[str]] = {}
    for group, dotted in GROUPS.items():
        try:
            available[group] = trait_fields(dotted)
        except Exception as exc:  # noqa: BLE001
            print(f"FATAL: cannot inspect {dotted}: {type(exc).__name__}: {exc}")
            return 2

    try:
        available["status"] |= trait_fields(Q10_STATUS)
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: cannot inspect {Q10_STATUS}: {type(exc).__name__}: {exc}")
        return 2

    # Fail early when the container used to re-parse ``.status.<enum member>``
    # reads cannot be inspected.
    try:
        field_type(Q10_CONTAINER, "status")
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: cannot inspect {Q10_CONTAINER}: {type(exc).__name__}: {exc}")
        return 2

    pattern = re.compile(r"\.(" + "|".join(GROUPS) + r")\.([a-z_][a-z0-9_]*)")

    missing: list[tuple[str, int, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(COMPONENT.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for match in pattern.finditer(line):
                group, field = match.group(1), match.group(2)
                if field in ALLOWED_NON_FIELDS:
                    continue
                key = (group, field)
                if key in seen:
                    continue
                seen.add(key)

                if field not in available[group]:
                    # Re-parse ``.status.value`` as container field ``status``
                    # (typed ``YXDeviceState``) plus the enum's ``value``.
                    if group == "status":
                        container_value = field_type(Q10_CONTAINER, "status")
                        if container_value is not None and exposes(
                            container_value, field
                        ):
                            continue
                    missing.append((str(path.relative_to(ROOT)), lineno, group, field))

    print(f"data fields referenced: {len(seen)}")
    print(f"missing: {len(missing)}")
    for src, lineno, group, field in missing:
        print(f"  - {src}:{lineno}  .{group}.{field}")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
