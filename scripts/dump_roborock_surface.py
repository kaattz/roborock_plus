"""Dump the public API surface of the python-roborock symbols this component uses.

Run under two interpreters and diff the JSON to find behavioral drift that an
import check cannot see (renamed enum members, changed signatures, new/removed
dataclass fields)::

    .analysis/venv551/Scripts/python.exe  scripts/dump_roborock_surface.py .analysis/surface-5.5.1.json
    .analysis/venv712/Scripts/python.exe  scripts/dump_roborock_surface.py .analysis/surface-7.12.0.json
"""

from __future__ import annotations

import ast
import enum
import importlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "roborock_plus"
WATCHED_PREFIXES = ("roborock", "vacuum_map_parser_base", "vacuum_map_parser_roborock")
LOCAL_MODULES = {"roborock_storage"}


def is_watched(module: str) -> bool:
    return module not in LOCAL_MODULES and module.startswith(WATCHED_PREFIXES)


def collect() -> dict[str, list[str]]:
    """Map module -> sorted imported symbol names ('' means module imported bare)."""
    wanted: dict[str, set[str]] = {}
    for path in sorted(COMPONENT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not is_watched(node.module):
                continue
            bucket = wanted.setdefault(node.module, set())
            for alias in node.names:
                if alias.name != "*":
                    bucket.add(alias.name)
    return {m: sorted(s) for m, s in sorted(wanted.items())}


def describe(name: str, obj: object) -> object:
    """Describe one symbol in a JSON-serialisable, diff-friendly way."""
    if inspect.isclass(obj):
        out: dict[str, object] = {"kind": "class"}
        # Enum members are the highest-risk surface for this component.
        if issubclass(obj, enum.Enum):
            out["kind"] = "enum"
            out["members"] = {m.name: m.value for m in obj}
            return out
        members: dict[str, object] = {}
        for attr, value in sorted(vars(obj).items()):
            if attr.startswith("_") and not attr.startswith("__"):
                continue
            if attr.startswith("__"):
                continue
            if callable(value):
                try:
                    members[attr] = str(inspect.signature(value))
                except (TypeError, ValueError):
                    members[attr] = "<no signature>"
            elif isinstance(value, (str, int, float, bool)) or value is None:
                members[attr] = repr(value)
            else:
                members[attr] = f"<{type(value).__name__}>"
        # Dataclass / annotated fields, including class-level annotations.
        annotations = getattr(obj, "__annotations__", None)
        if annotations:
            members["__annotations__"] = {k: str(v) for k, v in sorted(annotations.items())}
        out["members"] = dict(sorted(members.items()))
        return out
    if inspect.isroutine(obj):
        try:
            return {"kind": "routine", "signature": str(inspect.signature(obj))}
        except (TypeError, ValueError):
            return {"kind": "routine", "signature": "<no signature>"}
    if isinstance(obj, enum.Enum):
        return {"kind": "enum_member", "value": obj.value}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return {"kind": "value", "repr": repr(obj)}
    return {"kind": type(obj).__name__}


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".analysis" / "surface.json"
    from importlib.metadata import version

    result: dict[str, object] = {
        "python_roborock": version("python-roborock"),
        "modules": {},
    }
    want = collect()
    for module, symbols in want.items():
        entry: dict[str, object] = {"import_ok": True, "symbols": {}}
        try:
            mod = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - record any import failure as data
            entry["import_ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
            result["modules"][module] = entry  # type: ignore[index]
            continue
        for symbol in symbols:
            if not hasattr(mod, symbol):
                entry["symbols"][symbol] = {"kind": "MISSING"}  # type: ignore[index]
                continue
            try:
                entry["symbols"][symbol] = describe(symbol, getattr(mod, symbol))  # type: ignore[index]
            except Exception as exc:  # noqa: BLE001 - record any describe failure as data
                entry["symbols"][symbol] = {"kind": "ERROR", "error": repr(exc)}  # type: ignore[index]
        result["modules"][module] = entry  # type: ignore[index]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"wrote {out_path} for python-roborock {result['python_roborock']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
