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
            c, "leave", "binary_sensor.g20s_ultra_in_danger_zone"
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
        "let waits treat a timeout as success",
        lambda c: _drop_continue_on_timeout(c),
    ),
    (
        # The guard that separates a finished clean from a mid-task dock visit.
        # It sits in the conditions AND after the settle delay, so a mutation
        # that removes only one still leaves the door protected -- both go.
        "drop the task-end guard so a mid-task dock visit closes the door",
        lambda c: _drop_mentioning(c, 2, "sensor.sao_di_ji_v2_timestamp_2"),
    ),
    (
        # The defect fixed on 2026-10-10: a door wait satisfied by the command
        # echo, so the anti-crush guards in the closing phases never held.
        "drop the settle delay in front of the door waits",
        lambda c: _drop_door_wait_delays(c),
    ),
    (
        "shrink the door settle back inside the echo window",
        lambda c: _shrink_door_settle(c, 5),
    ),
    (
        # The gap found on 2026-10-13: the resume had no failure handling, so a
        # resume that did not happen was announced as success and the robot sat
        # paused behind a closed door with nobody told.
        "announce a resume that was never verified",
        lambda c: _drop_resume_verification(c),
    ),
    (
        "let a resume failure end the run without telling the owner",
        lambda c: _drop_resume_alert(c),
    ),
    (
        # `continue_on_error` cannot cover ServiceNotFound -- HA re-raises it --
        # so removing the pre-check is what makes an unloaded integration abort
        # the run before any alert.
        "call the resume without checking the integration is loaded",
        lambda c: _drop_resume_precheck(c),
    ),
    (
        "drop continue_on_error so a resume refusal kills the run",
        lambda c: _drop_resume_continue_on_error(c),
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


def _shrink_door_settle(config: dict, seconds: int) -> None:
    """Shorten every settle delay to `seconds`, restoring the echo window.

    Before 2026-10-10 the door waits had no delay at all, so the device's
    immediate echo of the command satisfied them. Five seconds is the delay the
    auto-unlock used while it was parking the door part-way open; it is inside
    the echo window just as surely as zero is.
    """
    for node in _walk(config):
        if "delay" in node and isinstance(node["delay"], str):
            parts = [int(p) for p in node["delay"].split(":")]
            total = parts[0] * 3600 + parts[1] * 60 + parts[2]
            if total > seconds:
                node["delay"] = f"00:00:{seconds:02d}"


def _drop_door_wait_delays(config: dict) -> None:
    """Remove the delay that sits in front of each door position wait.

    Recursive, because the parking branch keeps its door wait inside `if/then`.
    An earlier version walked only `branch["sequence"]`, so it removed two of the
    three delays -- the parking anti-crush guard stayed in place and the mutation
    escaped, which is the same blind spot the test's scan had.

    Targets only a delay immediately preceding a `wait_template`, so the
    unrelated 8-second settle before the parking re-check survives and this
    mutation tests exactly one thing.
    """

    def is_door_wait(step: object) -> bool:
        return (
            isinstance(step, dict)
            and "wait_template" in step
            and "current_position" in step["wait_template"]
        )

    def visit_list(items: list) -> list:
        keep = []
        for index, step in enumerate(items):
            if (
                isinstance(step, dict)
                and "delay" in step
                and index + 1 < len(items)
                and is_door_wait(items[index + 1])
            ):
                continue
            keep.append(step)
        for step in keep:
            visit(step)
        return keep

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if isinstance(value, list):
                    node[key] = visit_list(value)
                elif isinstance(value, dict):
                    visit(value)

    for branch in config["actions"][0]["choose"]:
        branch["sequence"] = visit_list(branch["sequence"])


def _drop_mentioning(config: dict, branch: int, needle: str) -> None:
    """Remove every condition or sequence step that references `needle`.

    A guard repeated in two places (the branch conditions and the re-check after
    the settle delay) must be removed from both, or the mutation is not a real
    weakening and the suite would rightly still pass.
    """
    target = config["actions"][0]["choose"][branch]
    target["conditions"] = [
        c for c in target["conditions"] if needle not in json.dumps(c, ensure_ascii=False)
    ]
    target["sequence"] = [
        s
        for s in target["sequence"]
        if needle not in json.dumps(s, ensure_ascii=False)
    ]


def _drop_continue_on_timeout(config: dict) -> None:
    for node in _walk(config):
        if "wait_template" in node:
            node.pop("continue_on_timeout", None)


def _find_resume_block(config: dict) -> dict:
    """Return phase B's guarded resume step.

    Raises when it is gone, so a mutation that cannot find its target fails
    loudly instead of silently reporting a caught mutation. The harness having
    its own bug is a recorded failure mode here: an earlier `_walk` missed dicts
    inside lists and reported "no target" for a mutation that should have run.
    """
    for step in config["actions"][0]["choose"][0]["sequence"]:
        if "roborock_plus.resume_task" in json.dumps(step, ensure_ascii=False):
            return step
    raise AssertionError("phase B no longer has a roborock_plus.resume_task step")


def _drop_resume_verification(config: dict) -> None:
    """Remove the wait that proves the robot actually left `paused`.

    This is the defect itself: the notice then reports a resume on the strength
    of the action not having raised, which is exactly what a paused robot that
    never moved also looks like.
    """
    block = _find_resume_block(config)["then"]
    remaining = [step for step in block if "wait_template" not in step]
    if len(remaining) == len(block):
        raise AssertionError("no verification wait found inside the resume block")
    _find_resume_block(config)["then"] = remaining


def _drop_resume_alert(config: dict) -> None:
    """Strip the alert from the verification failure path.

    Leaves the wait and the stop: the run still refuses to claim success, so
    only the "tell the owner" half is removed. That is the difference between a
    silent failure and a reported one, and it is the half that keeps a paused
    robot from looking like a working one.
    """
    block = _find_resume_block(config)["then"]
    guards = [step for step in block if "if" in step]
    if not guards:
        raise AssertionError("no guard found inside the resume block")
    guards[-1]["then"] = [
        step
        for step in guards[-1]["then"]
        if "alert_notify" not in json.dumps(step, ensure_ascii=False)
    ]


def _drop_resume_precheck(config: dict) -> None:
    """Keep the call but drop the `unavailable` check in front of it.

    `continue_on_error` does not cover `ServiceNotFound`, so without this the
    run dies at the call and never reaches the alert that would have said the
    integration is missing.
    """
    _find_resume_block(config).pop("if", None)


def _drop_resume_continue_on_error(config: dict) -> None:
    """Remove `continue_on_error` from the resume call.

    `resume_not_available` is raised when the device state cannot say how to
    continue the task -- the documented consequence of a restart while paused.
    """
    for step in _find_resume_block(config)["then"]:
        if step.get("action") == "roborock_plus.resume_task":
            step.pop("continue_on_error", None)
            return
    raise AssertionError("no resume call found to unprotect")


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
