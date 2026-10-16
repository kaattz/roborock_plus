"""Audit the integration's imports of python-roborock against an installed version.

Parses every module under ``custom_components/roborock_plus`` for
``from roborock... import X`` / ``from vacuum_map_parser... import X`` statements and
checks each imported symbol against the interpreter this script runs under.

Run it with the interpreter that has the target python-roborock installed::

    .analysis/venv712/Scripts/python.exe scripts/audit_roborock_api.py

Exit code is 1 when any symbol is missing, so it can gate a migration step.
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "roborock_plus"

# Modules whose symbols the component depends on, plus the modules themselves.
WATCHED_PREFIXES = ("roborock", "vacuum_map_parser_base", "vacuum_map_parser_roborock")

# ``roborock_storage`` is the component's own sibling module, not part of the
# third-party distribution; a bare prefix match would report it as missing.
LOCAL_MODULES = {"roborock_storage"}


def is_watched(module: str) -> bool:
    if module in LOCAL_MODULES:
        return False
    return module.startswith(WATCHED_PREFIXES)


def iter_imports() -> list[tuple[str, str, int, str]]:
    """Yield (module, symbol, lineno, file) for every watched import."""
    found: list[tuple[str, str, int, str]] = []
    for path in sorted(COMPONENT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not is_watched(node.module):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                found.append(
                    (
                        node.module,
                        alias.name,
                        node.lineno,
                        str(path.relative_to(ROOT)),
                    )
                )
            if not node.names:
                found.append((node.module, "", node.lineno, str(path.relative_to(ROOT))))
    return found


def main() -> int:
    try:
        import roborock  # noqa: F401
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - environment guard
        print(f"FATAL: python-roborock is not importable: {exc!r}")
        return 2

    try:
        from importlib.metadata import version

        pkg_version = version("python-roborock")
    except Exception:  # noqa: BLE001  # pragma: no cover
        pkg_version = "unknown"

    imports = iter_imports()
    modules: dict[str, str | None] = {}
    missing: list[dict[str, object]] = []

    for module, symbol, lineno, src in imports:
        if module not in modules:
            try:
                importlib.import_module(module)
                modules[module] = None
            except Exception as exc:  # noqa: BLE001 - report any import failure as a finding
                modules[module] = f"{type(exc).__name__}: {exc}"
        error = modules[module]
        if error is not None:
            missing.append(
                {
                    "kind": "module",
                    "module": module,
                    "symbol": symbol,
                    "file": src,
                    "line": lineno,
                    "error": error,
                }
            )
            continue
        if not symbol:
            continue
        obj = importlib.import_module(module)
        if not hasattr(obj, symbol):
            missing.append(
                {
                    "kind": "symbol",
                    "module": module,
                    "symbol": symbol,
                    "file": src,
                    "line": lineno,
                    "error": "attribute not present",
                }
            )

    print(f"python-roborock: {pkg_version}")
    print(f"watched imports: {len(imports)} across {len({i[3] for i in imports})} files")
    print(f"modules probed:  {len(modules)}")
    broken_modules = {m: e for m, e in modules.items() if e}
    print(f"unimportable modules: {len(broken_modules)}")
    for module, error in sorted(broken_modules.items()):
        print(f"  - {module}: {error}")

    print(f"missing symbols: {len(missing)}")
    for item in missing:
        if item["kind"] == "module":
            continue
        print(f"  - {item['module']}.{item['symbol']}  ({item['file']}:{item['line']})")

    (ROOT / ".analysis").mkdir(exist_ok=True)
    report = ROOT / ".analysis" / "roborock_api_audit.json"
    report.write_text(
        json.dumps(
            {
                "python_roborock": pkg_version,
                "missing": missing,
                "unimportable_modules": broken_modules,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"report: {report.relative_to(ROOT)}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
