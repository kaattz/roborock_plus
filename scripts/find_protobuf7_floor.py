"""Find the oldest python-roborock release whose protobuf requirement allows 7.x.

HA 2026.10.1 pins ``protobuf==7.36.0``. The component's current pin
(``python-roborock==5.5.1``) requires ``protobuf<7``, so the requirement can
never be satisfied. This prints the protobuf constraint per release so the
narrowest viable bump can be chosen.

    python scripts/find_protobuf7_floor.py
"""

from __future__ import annotations

import json
import urllib.request

VERSIONS = [
    "5.5.1",
    "5.5.2",
    "5.6.0",
    "6.0.0",
    "6.1.0",
    "6.2.0",
    "6.3.0",
    "6.4.0",
    "6.5.0",
    "6.5.1",
    "7.0.0",
    "7.1.0",
    "7.2.0",
    "7.9.0",
    "7.10.0",
    "7.11.0",
    "7.12.0",
    "7.12.3",
]


def protobuf_constraint(version: str) -> str:
    url = f"https://pypi.org/pypi/python-roborock/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=25) as response:
            data = json.load(response)
    except Exception as exc:  # noqa: BLE001 - report, do not mask
        return f"unavailable ({type(exc).__name__})"
    requires = data["info"].get("requires_dist") or []
    found = [r for r in requires if r.lower().startswith("protobuf")]
    return found[0] if found else "NOT DECLARED"


def main() -> int:
    print(f"{'version':<10} protobuf requirement")
    print("-" * 60)
    for version in VERSIONS:
        print(f"{version:<10} {protobuf_constraint(version)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
