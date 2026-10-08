"""Check that the garage state-machine tests actually catch the real defects.

A test that cannot fail is documentation, not a guard. Each mutation below
reintroduces a defect that was either observed in production or is the
plausible next mistake, and the suite must go red for it.

The `from: off` mutation is the important one: it is the exact defect the
full-chain run exposed, and if the suite does not catch it the fix is not
protected.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "scripts" / "garage_state_machine" / "state_machine.json"
TESTS = ["tests/test_garage_state_machine.py"]
TIMEOUT_SECONDS = 120

MUTATIONS = [
    (
        "restore the flapping trigger (drop from: off)",
        lambda c: _drop_trigger_key(c, "leave", "from"),
    ),
    (
        "key the departure on the inverted sensor",
        lambda c: _set_trigger_entity(
            c, "leave", "binary_sensor.g20s_ultra_in_safe_zone"
        ),
    ),
    (
        "drop the sustained requirement on return",
        lambda c: _drop_trigger_key(c, "return", "for"),
    ),
    (
        "let a docked robot count as having left",
        lambda c: _remove_condition(c, 0, "not"),
    ),
    (
        "drop the door-open condition that makes a repeat harmless",
        lambda c: _remove_numeric_condition(c, 0, "above"),
    ),
    (
        "close onto the robot when the sensor does not confirm it is inside",
        lambda c: _force_close(c, 2),
    ),
    (
        "close the door after a timeout alert",
        lambda c: _append_close_after_alert(c, 0),
    ),
    (
        "remove the settle delay before parking close",
        lambda c: _drop_delay(c, 2),
    ),
    (
        "allow waits to treat a timeout as success",
        lambda c: _drop_continue_on_timeout(c),
    ),
]


def _drop_trigger_key(config: dict, trigger_id: str, key: str) -> None:
    for trigger in config["triggers"]:
        if trigger.get("id") == trigger_id:
            trigger.pop(key, None)


def _set_trigger_entity(config: dict, trigger_id: str, entity: str) -> None:
    for trigger in config["triggers"]:
        if trigger.get("id") == trigger_id:
            trigger["entity_id"] = entity


def _remove_condition(config: dict, branch: int, kind: str) -> None:
    conditions = config["actions"][0]["choose"][branch]["conditions"]
    config["actions"][0]["choose"][branch]["conditions"] = [
        c for c in conditions if c.get("condition") != kind
    ]


def _remove_numeric_condition(config: dict, branch: int, key: str) -> None:
    conditions = config["actions"][0]["choose"][branch]["conditions"]
    config["actions"][0]["choose"][branch]["conditions"] = [
        c for c in conditions if key not in c
    ]


def _walk(node: object):
    """Yield every dict in a nested structure, descending into lists.

    Both containers must be traversed: an earlier version iterated only
    `dict.values()`, so a dict inside a list was never visited and a mutation
    claimed no target existed.
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _force_close(config: dict, branch: int) -> None:
    """Move the danger-zone check out of the if, so the door always closes."""
    block = config["actions"][0]["choose"][branch]["sequence"][-1]
    if "if" in block:
        block.pop("if")
        block.pop("else", None)


def _append_close_after_alert(config: dict, branch: int) -> None:
    """Add a close to the alert path, so a failure still moves the door.

    Matched on a substring: the config's action is `script.alert_notify`, and an
    earlier exact comparison with `alert_notify` silently found nothing.
    """
    for node in _walk(config["actions"][0]["choose"][branch]):
        action = node.get("action")
        if isinstance(action, str) and "alert_notify" in action:
            node["then_close"] = {"action": "cover.close_cover"}
            return
    raise AssertionError("no alert_notify found to mutate")


def _drop_delay(config: dict, branch: int) -> None:
    sequence = config["actions"][0]["choose"][branch]["sequence"]
    config["actions"][0]["choose"][branch]["sequence"] = [
        step for step in sequence if "delay" not in step
    ]


def _drop_continue_on_timeout(config: dict) -> None:
    for node in _walk(config):
        if "wait_template" in node:
            node.pop("continue_on_timeout", None)


def main() -> int:
    original = CONFIG.read_text(encoding="utf-8")
    caught = 0
    escaped = []

    try:
        for name, mutate in MUTATIONS:
            config = json.loads(original)
            mutate(config)
            CONFIG.write_text(
                json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", *TESTS, "-q", "--no-header"],
                    cwd=REPO,
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                # A hang is itself a detected failure; the guard is present but
                # behaves so differently the suite never settles.
                print(f"CAUGHT  {name}  (suite timed out)")
                caught += 1
                continue
            if result.returncode != 0:
                print(f"CAUGHT  {name}")
                caught += 1
            else:
                print(f"ESCAPED {name}")
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
