"""The auto-unlock must pause a *stopped* door, not a moving one.

Purpose (confirmed by the owner): release the clutch once the door is fully
shut, so it can be opened by hand. `cover.stop_cover` achieves that on a door
that has finished travelling. Issued while the door is still moving it does the
opposite -- it parks the door part-way open.

Measured 2026-10-10: the trigger fires on the device's echo (position 0 within
0.8s of the close command), then a 5-second delay, then stop_cover lands about a
fifth of the way through a 35-second travel. The door came to rest at 77 and at
38 in two separate runs.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
AUTOMATION = REPO / "automations" / "vacuum_garage_door_auto_unlock.yaml"
TIMING = REPO / "custom_components" / "roborock_plus" / "door_timing.py"


def _config() -> dict:
    return yaml.safe_load(AUTOMATION.read_text(encoding="utf-8"))


def _echo_settle() -> int:
    """Load the timing constant by path; the package cannot be imported here."""
    spec = importlib.util.spec_from_file_location("door_timing_for_auto_unlock", TIMING)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ECHO_SETTLE_SECONDS


def _delay_seconds(delay: object) -> int:
    """Read a HA delay, which may be {seconds: N} or 'HH:MM:SS'."""
    if isinstance(delay, dict):
        return int(delay["seconds"])
    hours, minutes, seconds = (int(p) for p in str(delay).split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _first_delay() -> int:
    for action in _config()["actions"]:
        if "delay" in action:
            return _delay_seconds(action["delay"])
    raise AssertionError("no delay in the automation")


def test_unique_id_is_preserved() -> None:
    """`id` is the HA unique_id; changing it orphans the entity's history."""
    assert _config()["id"] == "1788771256556"


def test_the_delay_comes_before_the_stop() -> None:
    """Order matters, and it is the whole fix.

    Asserting only that *some* delay is long enough is not enough: moving the
    delay to after the guarded stop leaves `_first_delay()` still >= 45 while the
    PAUSE lands at t~0, which is the original defect in a new shape. That escape
    was measured -- the suite stayed green. This pins the order.
    """
    actions = _config()["actions"]
    delay_indexes = [i for i, step in enumerate(actions) if "delay" in step]
    stop_indexes = [
        i
        for i, step in enumerate(actions)
        if "cover.stop_cover" in json.dumps(step, ensure_ascii=False)
    ]
    assert delay_indexes, "the automation must wait out the echo window"
    assert stop_indexes, "the automation must still pause the door"

    assert min(delay_indexes) < min(stop_indexes), (
        "the settle delay must run BEFORE the stop; otherwise the door is paused "
        f"while still travelling (delay at {delay_indexes}, stop at {stop_indexes})"
    )


def test_pause_happens_after_a_full_travel() -> None:
    assert _first_delay() >= _echo_settle(), (
        "stop_cover must land after the door has stopped moving; a short delay "
        "issues PAUSE mid-travel and parks the door part-way open"
    )


def test_stop_is_guarded_by_a_position_recheck() -> None:
    """The door may not have shut; do not fire PAUSE at an unknown position.

    Structural rather than string-search: the recheck must be the `if` of the
    step that stops the cover, and the stop must live in its `then` branch. A
    substring scan for "condition" would also pass if the recheck sat in the
    `else`, or in some unrelated step.
    """
    actions = _config()["actions"]
    stops = [
        step
        for step in actions
        if "cover.stop_cover" in json.dumps(step.get("then", []), ensure_ascii=False)
    ]
    assert stops, "stop_cover must be inside a then branch, not an unconditional step"

    step = stops[0]
    assert step.get("if"), "the stop must be conditional"
    conditions = json.dumps(step["if"], ensure_ascii=False)
    assert "current_position" in conditions, (
        "the condition guarding the stop must be a fresh position recheck"
    )
    assert "below" in conditions, (
        "the recheck must require the door to be shut, not merely positioned"
    )


def test_the_stop_is_not_unconditional() -> None:
    """A bare stop_cover action would fire even when the door never shut."""
    for step in _config()["actions"]:
        assert step.get("action") != "cover.stop_cover", (
            "stop_cover must sit behind the recheck; an unconditional stop is "
            "the original defect in a new place"
        )
