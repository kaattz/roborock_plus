"""Mutation check: break the fix six ways and confirm the suite notices.

A passing suite proves nothing about a safety interlock unless the tests fail
when the interlock is removed. Each mutation is applied to the real file, the
suite is run, and the file is restored.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "roborock_plus"

MUTATIONS = [
    (
        "revert to the raw map position in the sensors",
        COMPONENT / "binary_sensor.py",
        "    resolved = coordinator.resolve_vacuum_position()\n"
        "    if not resolved.trusted or resolved.x is None or resolved.y is None:\n"
        "        return None\n"
        "    return resolved.x, resolved.y\n",
        "    map_data = coordinator.properties_api.map_content.map_data\n"
        "    if map_data is None or map_data.vacuum_position is None:\n"
        "        return None\n"
        "    return map_data.vacuum_position.x, map_data.vacuum_position.y\n",
    ),
    (
        "let an untrusted sample through anyway",
        COMPONENT / "binary_sensor.py",
        "    if not resolved.trusted or resolved.x is None or resolved.y is None:\n"
        "        return None\n",
        "    if resolved.x is None or resolved.y is None:\n"
        "        return None\n",
    ),
    (
        "stop passing the charger marker",
        COMPONENT / "coordinator.py",
        "            charger=None if map_data is None else map_data.charger,\n",
        "            charger=None,\n",
    ),
    (
        "stop passing the state (docked check never fires)",
        COMPONENT / "coordinator.py",
        "            state=self.properties_api.status.state,\n",
        "            state='cleaning',\n",
    ),
    (
        "report stale from read success again",
        COMPONENT / "vacuum.py",
        '            "stale": (not refreshed) or resolved.from_dock,\n',
        '            "stale": not refreshed,\n',
    ),
    (
        "ignore the dock substitution when flagging stale",
        COMPONENT / "vacuum.py",
        '            "stale": (not refreshed) or resolved.from_dock,\n',
        '            "stale": resolved.from_dock,\n',
    ),
    (
        "drop the freshness gate in the resolver",
        COMPONENT / "coordinator.py",
        '            return ResolvedPosition(None, None, False, "sample_too_old")\n',
        "            pass\n",
    ),
    (
        "stop recording the service's own fresh read",
        COMPONENT / "vacuum.py",
        "        if refreshed:\n            self.coordinator.note_map_position_sample()\n",
        "        if False:\n            self.coordinator.note_map_position_sample()\n",
    ),
    (
        "stop recording the editor's own fresh read",
        COMPONENT / "vacuum.py",
        "        if await self._async_try_refresh_map_content(map_content_trait):\n"
        "            self.coordinator.note_map_position_sample()\n",
        "        await self._async_try_refresh_map_content(map_content_trait)\n",
    ),
]


def run_suite() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--no-header", "-x"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


failures = []
for label, path, original, mutated in MUTATIONS:
    text = path.read_text(encoding="utf-8")
    if original not in text:
        failures.append(f"SETUP FAILED (anchor missing): {label}")
        continue
    path.write_text(text.replace(original, mutated, 1), encoding="utf-8")
    try:
        code, out = run_suite()
    finally:
        path.write_text(text, encoding="utf-8")
    status = "CAUGHT" if code != 0 else "ESCAPED"
    if code == 0:
        failures.append(label)
    print(f"{status:8} {label}")

print()
if failures:
    print("ESCAPED MUTATIONS (tests are too weak):")
    for item in failures:
        print(" -", item)
    sys.exit(1)
print(f"all {len(MUTATIONS)} mutations caught")
