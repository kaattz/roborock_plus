"""Check whether the stale docked position can reach the stuck detector.

Two state tables describe the same hardware, and a state appearing in both
would be a real bug: `v1_position_trust` would substitute the charger marker
for a docked robot's position, and `v1_stuck_detection` would then read that
unchanging value as "not moving" and eventually declare the robot stuck --
stopping a clean that was working fine.

The two tables answer different questions, so they are allowed to be wrong in
opposite directions rather than forced to agree on every state:

* `DOCKED_STATE_NAMES` includes an ambiguous state, because missing a docked
  robot can let the door close on it.
* `MOVEMENT_STATES` excludes that same state, because assessing a docked robot
  can stop a working clean.

What is not allowed is overlapping, so this asserts disjointness.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

COMPONENT = Path(r"C:\Code\roborock_plus\custom_components\roborock_plus")


def load(name: str, filename: str):
    """Load a component module without importing the package."""
    spec = importlib.util.spec_from_file_location(name, COMPONENT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


trust = load("trust_under_test", "v1_position_trust.py")
stuck = load("stuck_under_test", "v1_stuck_detection.py")

docked = trust.DOCKED_STATE_NAMES
movement = stuck.MOVEMENT_STATES
overlap = docked & movement

print(f"docked states:   {len(docked)}")
print(f"movement states: {len(movement)}")
print(f"overlap:         {sorted(overlap) or '(none)'}")
print()

AMBIGUOUS = "back_to_dock_washing_duster"
print(f"ambiguous state {AMBIGUOUS!r}:")
print(f"  docked?   {trust.is_docked_state(AMBIGUOUS)}  (fail-safe: door stays open)")
print(f"  assessed? {stuck.should_assess_movement(state=AMBIGUOUS)}  (fail-safe: no false stop)")
print()

# States the robot actually reported, from 30 days of history on this device.
OBSERVED_DOCKED = ["charging", "attaching_the_mop", "detaching_the_mop", "docking"]
for state in OBSERVED_DOCKED:
    print(
        f"  {state:20} docked={trust.is_docked_state(state)!s:5} "
        f"assessed={stuck.should_assess_movement(state=state)}"
    )
print()

failures = []
if overlap:
    failures.append(
        f"states in both tables: {sorted(overlap)} -- a docked robot's position "
        "would be read as evidence of standing still"
    )
if trust.is_docked_state(AMBIGUOUS) and stuck.should_assess_movement(state=AMBIGUOUS):
    failures.append(f"{AMBIGUOUS} must not be both docked and assessed")
if not trust.is_docked_state("charging"):
    failures.append("charging must be docked")
if not stuck.should_assess_movement(state="cleaning"):
    failures.append("cleaning must be assessed")
if stuck.should_assess_movement(state="docking") is not True:
    failures.append("docking is travel and must be assessed")

if failures:
    print("FAILED:")
    for item in failures:
        print(" -", item)
    sys.exit(1)

print("PASS: the two tables are disjoint; a docked robot's position cannot reach")
print("      the stuck detector.")
