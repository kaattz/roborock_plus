"""Diff two ``dump_roborock_surface.py`` outputs and print the behavioral drift.

Usage::

    python scripts/diff_roborock_surface.py .analysis/surface-5.5.1.json .analysis/surface-7.12.0.json

Only reports symbols this component actually imports, so the output stays short
enough to review line by line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    left, right = load(sys.argv[1]), load(sys.argv[2])
    print(f"left : python-roborock {left['python_roborock']}")
    print(f"right: python-roborock {right['python_roborock']}")

    for label, payload in (("LEFT", left), ("RIGHT", right)):
        broken = {
            m: e.get("error")
            for m, e in payload["modules"].items()
            if not e.get("import_ok")
        }
        if broken:
            print(f"\n{label}: unimportable modules: {broken}")

    removed_symbols: list[str] = []
    new_symbols: list[str] = []
    enum_drift: list[str] = []
    member_drift: list[str] = []
    value_drift: list[str] = []
    kind_drift: list[str] = []

    for module in sorted(set(left["modules"]) | set(right["modules"])):
        lm = left["modules"].get(module, {})
        rm = right["modules"].get(module, {})
        ls, rs = lm.get("symbols", {}), rm.get("symbols", {})
        for symbol in sorted(set(ls) | set(rs)):
            a, b = ls.get(symbol), rs.get(symbol)
            if a is None:
                new_symbols.append(f"{module}.{symbol}")
                continue
            if b is None:
                removed_symbols.append(f"{module}.{symbol}")
                continue
            if a.get("kind") != b.get("kind"):
                kind_drift.append(f"{module}.{symbol}: {a.get('kind')} -> {b.get('kind')}")
                continue
            kind = a.get("kind")
            if kind == "enum":
                am, bm = a.get("members", {}), b.get("members", {})
                for name in sorted(set(am) | set(bm)):
                    if name not in bm:
                        enum_drift.append(f"{module}.{symbol}.{name} REMOVED (was {am[name]!r})")
                    elif name not in am:
                        enum_drift.append(f"{module}.{symbol}.{name} ADDED ({bm[name]!r})")
                    elif am[name] != bm[name]:
                        enum_drift.append(f"{module}.{symbol}.{name}: {am[name]!r} -> {bm[name]!r}")
            elif kind == "class":
                am, bm = a.get("members", {}), b.get("members", {})
                for name in sorted(set(am) | set(bm)):
                    if name not in bm:
                        member_drift.append(f"{module}.{symbol}.{name} REMOVED (was {am[name]})")
                    elif name not in am:
                        member_drift.append(f"{module}.{symbol}.{name} ADDED ({bm[name]})")
                    elif am[name] != bm[name]:
                        member_drift.append(f"{module}.{symbol}.{name}: {am[name]} -> {bm[name]}")
            elif kind == "routine":
                if a.get("signature") != b.get("signature"):
                    value_drift.append(
                        f"{module}.{symbol}: {a.get('signature')} -> {b.get('signature')}"
                    )
            elif kind == "value":
                if a.get("repr") != b.get("repr"):
                    value_drift.append(f"{module}.{symbol}: {a.get('repr')} -> {b.get('repr')}")

    def section(title: str, items: list[str]) -> None:
        print(f"\n=== {title} ({len(items)}) ===")
        for item in items:
            print(f"  {item}")

    section("REMOVED SYMBOLS (import-breaking)", removed_symbols)
    section("KIND CHANGED (import-breaking)", kind_drift)
    section("ENUM MEMBER DRIFT", enum_drift)
    section("CLASS MEMBER / SIGNATURE DRIFT", member_drift)
    section("ROUTINE SIGNATURE DRIFT", value_drift)
    section("NEW SYMBOLS (informational)", new_symbols)

    breaking = len(removed_symbols) + len(kind_drift)
    print(f"\nimport-breaking changes: {breaking}")
    print(f"behavioral-drift changes: {len(enum_drift) + len(member_drift) + len(value_drift)}")
    return 1 if breaking else 0


if __name__ == "__main__":
    sys.exit(main())
