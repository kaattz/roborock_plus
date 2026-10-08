"""Simulate the BLE button's press cycle against real door positions.

The blueprint this replaces is being retired, so the new automation has to be
shown to behave correctly *before* it goes live -- and I am not allowed to press
the physical button, so the check runs against recorded positions instead.

Every position below was observed on 2026-10-08 from
`cover.vacuum_garage_door`:

    0    closed
    11   stopped partway
    77   a resting partially-open position
    100  open

The simulation replays presses and asserts the action chosen for each, including
the case that must refuse: when the position is missing, the automation must not
issue a close.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "button_cycle.json").read_text(encoding="utf-8"))

DOOR = "cover.vacuum_garage_door"
BUTTON = "event.vacuum_garage_door_click"


def condition_holds(condition: dict, state: dict) -> bool:
    """Evaluate the native condition forms this config uses."""
    kind = condition.get("condition")
    if kind == "state":
        if "attribute" in condition:
            key = f"{condition['entity_id']}:{condition['attribute']}"
            return state.get(key) == condition["state"]
        expected = condition["state"]
        actual = state.get(condition["entity_id"])
        return actual in expected if isinstance(expected, list) else actual == expected
    if kind == "numeric_state":
        key = f"{condition['entity_id']}:{condition.get('attribute')}"
        raw = state.get(key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return False
        if "below" in condition and not value < condition["below"]:
            return False
        if "above" in condition and not value > condition["above"]:
            return False
        return True
    raise AssertionError(f"unsupported condition: {condition}")


def action_for(state: dict) -> str:
    """Return the action the automation would take, or 'none'."""
    block = CONFIG["actions"][0]
    if not all(condition_holds(c, state) for c in CONFIG["conditions"]):
        return "none"
    for branch in block["choose"]:
        if all(condition_holds(c, state) for c in branch["conditions"]):
            step = branch["sequence"][0]
            return step["action"]
    return "none"


# (label, door state, position, expected action)
CASES = [
    ("closed door", "closed", 0, "cover.open_cover"),
    ("closed, position 1 (rounding)", "closed", 1, "cover.open_cover"),
    ("partly open at 11", "open", 11, "cover.close_cover"),
    ("resting at 77", "open", 77, "cover.close_cover"),
    ("fully open", "open", 100, "cover.close_cover"),
    ("mid-travel, opening", "opening", 40, "cover.stop_cover"),
    ("mid-travel, closing", "closing", 60, "cover.stop_cover"),
    ("position missing", "open", None, "none"),
    ("position unavailable string", "open", "unavailable", "none"),
    ("cover unknown", "unknown", None, "none"),
]

print("BLE button press -> action, against recorded positions")
print()
failures = []
for label, door_state, position, expected in CASES:
    state = {
        DOOR: door_state,
        f"{DOOR}:current_position": position,
        BUTTON: "2026-10-08T05:37:50.687+00:00",
        f"{BUTTON}:event_type": "单击",
    }
    got = action_for(state)
    ok = got == expected
    mark = "ok " if ok else "BAD"
    shown = "None" if position is None else position
    print(f"  {mark} {label:32} pos={shown!s:12} -> {got}")
    if not ok:
        failures.append((label, expected, got))

print()
if failures:
    print("FAILED:")
    for label, expected, got in failures:
        print(f"  - {label}: wanted {expected}, got {got}")
    raise SystemExit(1)

print("PASS: every recorded position maps to the intended action.")
print()

# ---------------------------------------------------------------------------
# The safety property: an unknown position must never produce a close.
# ---------------------------------------------------------------------------

print("Safety: no branch may close the door on an unknown position")
unknown_states = [
    {"door": "open", "pos": None, "why": "missing attribute"},
    {"door": "open", "pos": "unavailable", "why": "integration offline"},
    {"door": "open", "pos": "unknown", "why": "state unknown"},
    {"door": "unknown", "pos": None, "why": "cover unavailable"},
    {"door": "unavailable", "pos": None, "why": "cover unavailable"},
]
for case in unknown_states:
    state = {
        DOOR: case["door"],
        f"{DOOR}:current_position": case["pos"],
        BUTTON: "2026-10-08T05:37:50.687+00:00",
        f"{BUTTON}:event_type": "单击",
    }
    got = action_for(state)
    status = "ok " if got != "cover.close_cover" else "BAD"
    print(f"  {status} {case['why']:24} -> {got}")
    if got == "cover.close_cover":
        failures.append((case["why"], "not close", got))

if failures:
    print()
    print("FAILED: an unknown position can close the door")
    raise SystemExit(1)
print()
print("PASS: an unknown position never closes the door.")

# ---------------------------------------------------------------------------
# The default branch must actually warn, not fail silently.
# ---------------------------------------------------------------------------

print()
print("The refusal branch must tell the owner")
block = CONFIG["actions"][0]
assert "default" in block, "a press with no usable position must not pass silently"
default_blob = json.dumps(block["default"], ensure_ascii=False)
assert "alert_notify" in default_blob, "the refusal must notify"
print("  ok  default branch sends alert_notify")

print()
print("Only a single click acts")
other = {
    DOOR: "closed",
    f"{DOOR}:current_position": 0,
    BUTTON: "2026-10-08T05:37:50.687+00:00",
    f"{BUTTON}:event_type": "双击",
}
got = action_for(other)
print(f"  {'ok ' if got == 'none' else 'BAD'} event_type=双击 -> {got}")
if got != "none":
    raise SystemExit(1)

print()
print("=" * 70)
print("ALL CHECKS PASS")
