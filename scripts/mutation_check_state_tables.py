"""Mutation check for the state-table disjointness guard.

Reintroduces the real bug -- `back_to_dock_washing_duster` in both tables --
and confirms the suite fails. A guard that does not catch the bug it was
written for is decoration.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "roborock_plus"

MUTATIONS = [
    (
        "re-add the ambiguous state to MOVEMENT_STATES",
        COMPONENT / "v1_stuck_detection.py",
        '        "zoned_clean_mop_mopping",\n    }\n)',
        '        "zoned_clean_mop_mopping",\n'
        '        "back_to_dock_washing_duster",\n    }\n)',
    ),
    (
        "drop charging from DOCKED_STATE_NAMES",
        COMPONENT / "v1_position_trust.py",
        '        "charging",\n        "charging_complete",\n',
        '        "charging_complete",\n',
    ),
    (
        "stop assessing cleaning",
        COMPONENT / "v1_stuck_detection.py",
        '        "cleaning",\n        "segment_cleaning",\n',
        '        "segment_cleaning",\n',
    ),
    (
        "treat docking as docked",
        COMPONENT / "v1_position_trust.py",
        '        "attaching_the_mop",\n        "detaching_the_mop",\n',
        '        "attaching_the_mop",\n        "detaching_the_mop",\n        "docking",\n',
    ),
]


def run_suite() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--no-header", "-x"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode


failures = []
for label, path, original, mutated in MUTATIONS:
    text = path.read_text(encoding="utf-8")
    if original not in text:
        failures.append(f"SETUP FAILED (anchor missing): {label}")
        print(f"{'SETUP?':8} {label}")
        continue
    path.write_text(text.replace(original, mutated, 1), encoding="utf-8")
    try:
        code = run_suite()
    finally:
        path.write_text(text, encoding="utf-8")
    print(f"{'CAUGHT' if code != 0 else 'ESCAPED':8} {label}")
    if code == 0:
        failures.append(label)

print()
if failures:
    print("ESCAPED MUTATIONS:")
    for item in failures:
        print(" -", item)
    sys.exit(1)
print(f"all {len(MUTATIONS)} mutations caught")
