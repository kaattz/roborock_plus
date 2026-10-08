"""Check that the button-cycle simulation catches the mistakes that matter.

The dangerous mistake for this automation is closing the door on an unknown
position, because that is the one direction that can hit the robot. The
simulation must go red if any mutation reintroduces it, and must also catch the
ordinary mistakes: an inverted open/close mapping, a missing stop branch, and a
refusal that fails silently.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# scripts/ -> repo root is one level up, not two: this file lives in scripts/
# and the config lives in scripts/garage_button_cycle/.
REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "scripts" / "garage_button_cycle" / "button_cycle.json"
SIM = REPO / "scripts" / "garage_button_cycle" / "simulate_presses.py"
TIMEOUT_SECONDS = 60

DOOR = "cover.vacuum_garage_door"


def _branches(config: dict) -> list:
    return config["actions"][0]["choose"]


def invert_open_close(config: dict) -> None:
    """Swap the open and close actions, so a closed door closes again."""
    branches = _branches(config)
    branches[1]["sequence"][0]["action"] = "cover.close_cover"
    branches[2]["sequence"][0]["action"] = "cover.open_cover"


def close_when_unknown(config: dict) -> None:
    """Widen the close branch so a missing position still closes."""
    branches = _branches(config)
    close_branch = branches[2]
    close_branch["conditions"] = [
        {
            "condition": "not",
            "conditions": [
                {
                    "condition": "state",
                    "entity_id": DOOR,
                    "state": ["opening", "closing"],
                }
            ],
        }
    ]


def drop_stop_branch(config: dict) -> None:
    """Remove the stop branch, so a press mid-travel opens or closes instead."""
    branches = _branches(config)
    del branches[0]


def silent_refusal(config: dict) -> None:
    """Make the no-position case do nothing without telling anyone."""
    config["actions"][0]["default"] = []


def allow_any_event(config: dict) -> None:
    """Drop the single-click condition, so any event type acts."""
    config["conditions"] = []


MUTATIONS = [
    ("invert open and close", invert_open_close),
    ("close on an unknown position", close_when_unknown),
    ("drop the stop branch", drop_stop_branch),
    ("refuse silently", silent_refusal),
    ("act on any event type", allow_any_event),
]


def main() -> int:
    original = CONFIG.read_text(encoding="utf-8")
    caught = 0
    escaped = []

    try:
        for name, mutate in MUTATIONS:
            config = json.loads(original)
            try:
                mutate(config)
            except (KeyError, IndexError) as err:
                print(f"SETUP FAILED  {name}: {err}")
                escaped.append(name)
                continue
            CONFIG.write_text(
                json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            try:
                result = subprocess.run(
                    [sys.executable, str(SIM)],
                    cwd=REPO,
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                print(f"CAUGHT        {name}  (timed out)")
                caught += 1
                continue
            if result.returncode != 0:
                print(f"CAUGHT        {name}")
                caught += 1
            else:
                print(f"ESCAPED       {name}")
                escaped.append(name)
    finally:
        CONFIG.write_text(original, encoding="utf-8")

    print()
    print(f"{caught}/{len(MUTATIONS)} mutations caught")
    if escaped:
        print("escaped:")
        for name in escaped:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
