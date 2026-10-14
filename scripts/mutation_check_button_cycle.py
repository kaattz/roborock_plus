"""Check that the button-cycle simulation catches the mistakes that matter.

The dangerous mistake is treating a press during travel as "reverse", because if
the door is opening and the robot is in the doorway, reversing means closing on
it. That is exactly what the previous version did: it read `opening`/`closing`,
which last a measured mean of 0.79s out of a ~34s travel, so the stop branch was
unreachable.

Every mutation below reintroduces a real defect -- one of them is the shipped bug
-- and the simulation must go red for each.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "scripts" / "garage_button_cycle" / "button_cycle.json"
SIM = REPO / "scripts" / "garage_button_cycle" / "simulate_presses.py"
TIMEOUT_SECONDS = 120

DOOR = "cover.vacuum_garage_door"
BUTTON = "event.vacuum_garage_door_click"
CONTACT = "binary_sensor.vacuum_garage_door_contact_state"
MOVING_END = "input_datetime.garage_door_moving_end_time"


def _press_choose(config: dict) -> list:
    """Return the inner choose of the press branch."""
    return config["actions"][0]["choose"][1]["sequence"][1]["choose"]


def _record_branch(config: dict) -> dict:
    return config["actions"][0]["choose"][0]


def trigger_on_cover_state(config: dict) -> None:
    """THE SHIPPED BUG: decide 'moving' from opening/closing.

    Those flags are cleared as soon as the echoed position changes (~0.5s), so a
    press mid-travel falls through to the reverse branch. This is the defect the
    rewrite exists to remove, so the simulation must catch it.
    """
    branches = _press_choose(config)
    branches[1]["conditions"] = [
        {
            "condition": "state",
            "entity_id": DOOR,
            "state": ["opening", "closing"],
        }
    ]
    # And drop the clock guard, so nothing else answers "moving".
    branches[0]["conditions"] = [{"condition": "template", "value_template": "false"}]


def drop_the_record_trigger(config: dict) -> None:
    """Stop recording the travel window, so nothing ever reads as moving."""
    config["triggers"] = [t for t in config["triggers"] if t["id"] != "command"]


def ignore_other_covers(config: dict) -> None:
    """Record for every cover command, not just this door's.

    A curtain moving elsewhere would mark the garage door as travelling, so the
    next press would stop a door that is at rest. Fails safe, but wrong.
    """
    _record_branch(config)["conditions"] = [
        {"condition": "trigger", "id": ["command"]}
    ]


def record_on_any_service(config: dict) -> None:
    """Record for any service at all, including non-cover ones."""
    conditions = _record_branch(config)["conditions"]
    _record_branch(config)["conditions"] = [
        c
        for c in conditions
        if not (
            c.get("condition") == "template"
            and "service" in str(c.get("value_template", ""))
        )
    ]


def close_when_unknown(config: dict) -> None:
    """Let an unknown reading reach the close branch."""
    branches = _press_choose(config)
    branches[0]["conditions"] = [{"condition": "template", "value_template": "false"}]
    branches[2]["conditions"] = [
        {
            "condition": "not",
            "conditions": [
                {"condition": "state", "entity_id": CONTACT, "state": "on"}
            ],
        }
    ]
    branches[3]["conditions"] = [{"condition": "template", "value_template": "false"}]


def invert_open_close(config: dict) -> None:
    """Swap open and close, so a shut door shuts again."""
    branches = _press_choose(config)
    branches[2]["sequence"][0]["action"] = "cover.close_cover"
    branches[3]["sequence"][0]["action"] = "cover.open_cover"


def drop_the_stop_branch(config: dict) -> None:
    """Remove the stop branch, so a press mid-travel reverses instead."""
    del _press_choose(config)[1]


def silent_refusal(config: dict) -> None:
    """Refuse on unknown readings without telling anyone."""
    _press_choose(config)[0]["sequence"] = []


def allow_any_event(config: dict) -> None:
    """Drop the single-click gate, so any event type acts."""
    press = config["actions"][0]["choose"][1]
    press["conditions"] = [c for c in press["conditions"] if c.get("condition") == "trigger"]


def use_single_mode(config: dict) -> None:
    """`single` drops the recording run triggered by our own command."""
    config["mode"] = "single"


def compare_naive_to_aware_datetimes(config: dict) -> None:
    """THE BUG THE SIMULATION MISSED: `now() < as_datetime(state)`.

    `as_datetime()` returns a naive datetime for a string with no offset while
    `now()` is timezone-aware, so the comparison raises TypeError at runtime. An
    exception in a condition aborts the run, so the stop branch is never reached
    and the door stops responding to the button -- with the fix apparently in
    place, and every structural check still green.

    Found by rendering the real template in Home Assistant after this suite had
    gone 11/11, which is why the simulation now renders every template too.
    """
    branches = _press_choose(config)
    branches[1]["conditions"] = [
        {
            "condition": "template",
            "value_template": "{{ now() < as_datetime(travel_end) }}",
        }
    ]


def arm_on_reload(config: dict) -> None:
    """Let a config reload enable a real door."""
    config["initial_state"] = True


MUTATIONS = [
    ("THE SHIPPED BUG: decide 'moving' from opening/closing", trigger_on_cover_state),
    ("THE MISSED BUG: compare a naive datetime to an aware one", compare_naive_to_aware_datetimes),
    ("stop recording the travel window", drop_the_record_trigger),
    ("record for every cover, not just this door", ignore_other_covers),
    ("record on any service at all", record_on_any_service),
    ("close on an unknown reading", close_when_unknown),
    ("invert open and close", invert_open_close),
    ("drop the stop branch", drop_the_stop_branch),
    ("refuse silently", silent_refusal),
    ("act on any event type", allow_any_event),
    ("use mode: single", use_single_mode),
    ("arm on reload", arm_on_reload),
]


def main() -> int:
    original = CONFIG.read_text(encoding="utf-8")
    caught = 0
    escaped = []

    try:
        for name, mutate in MUTATIONS:
            config = json.loads(original)
            before = json.dumps(config, sort_keys=True, ensure_ascii=False)
            try:
                mutate(config)
            except (KeyError, IndexError, TypeError) as err:
                print(f"SETUP FAILED  {name}: {err!r}")
                escaped.append(name)
                continue
            if json.dumps(config, sort_keys=True, ensure_ascii=False) == before:
                print(f"SETUP FAILED  {name}: mutation changed nothing")
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
