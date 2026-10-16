"""Resolve every attribute access on a python-roborock symbol to catch API drift.

An import check is not enough: ``RoborockDockTypeCode`` still imports fine in
7.12.0, but the member ``no_dock`` was removed. That only fails at runtime, in
the middle of a sensor update. This script walks the component's source,
tracks which local names are bound to library modules/classes, and resolves
each ``name.attr``/``name.attr.attr`` chain against the installed library.

Usage::

    .analysis/venv712/Scripts/python.exe scripts/check_roborock_attribute_use.py

Exit code 1 when any referenced attribute is missing.
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "roborock_plus"
WATCHED_PREFIXES = ("roborock", "vacuum_map_parser_base", "vacuum_map_parser_roborock")
LOCAL_MODULES = {"roborock_storage"}


def is_watched(module: str) -> bool:
    return module not in LOCAL_MODULES and module.startswith(WATCHED_PREFIXES)


def resolve_module(name: str) -> str | None:
    """Return *name* if it imports, else None."""
    try:
        importlib.import_module(name)
        return name
    except Exception:  # noqa: BLE001
        return None


def attribute_probe(module: str, path: list[str]) -> str | None:
    """Walk *path* from module; return an error string when it breaks."""
    try:
        obj: object = importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 - report any import failure as a finding
        return f"module import failed: {type(exc).__name__}: {exc}"
    for index, attr in enumerate(path):
        if not hasattr(obj, attr):
            so_far = ".".join(path[:index]) or "<module>"
            return f"{module}: {so_far} has no attribute {attr!r}"
        obj = getattr(obj, attr)
    return None


def main() -> int:
    from importlib.metadata import version

    try:
        pkg_version = version("python-roborock")
    except Exception:  # noqa: BLE001
        pkg_version = "unknown"

    findings: list[dict[str, object]] = []
    checked = 0

    for path in sorted(COMPONENT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        src = str(path.relative_to(ROOT))

        # Local name -> (module, path-so-far) for names bound from the library.
        bound: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and is_watched(node.module):
                if node.module in LOCAL_MODULES:
                    continue
                if resolve_module(node.module) is None:
                    findings.append(
                        {
                            "file": src,
                            "line": node.lineno,
                            "detail": f"module {node.module!r} does not import",
                        }
                    )
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    bound[alias.asname or alias.name] = [node.module, alias.name]

        if not bound:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            # Flatten the chain: a.b.c -> name "a", attrs ["b", "c"].
            attrs: list[str] = []
            cursor: ast.expr = node
            while isinstance(cursor, ast.Attribute):
                attrs.append(cursor.attr)
                cursor = cursor.value
            if not isinstance(cursor, ast.Name):
                continue
            base = cursor.id
            if base not in bound:
                continue
            module, *root = bound[base]
            attrs.reverse()
            probed = root + attrs
            checked += 1
            error = attribute_probe(module, probed)
            if error:
                findings.append(
                    {
                        "file": src,
                        "line": node.lineno,
                        "expression": f"{base}.{'.'.join(attrs)}",
                        "detail": error,
                    }
                )

    print(f"python-roborock: {pkg_version}")
    print(f"attribute chains resolved: {checked}")
    print(f"broken: {len(findings)}")
    for item in findings:
        expr = item.get("expression", "<import>")
        print(f"  - {item['file']}:{item['line']}  {expr}")
        print(f"      {item['detail']}")

    out = ROOT / ".analysis" / "roborock_attribute_audit.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {"python_roborock": pkg_version, "checked": checked, "broken": findings},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"report: {out.relative_to(ROOT)}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
