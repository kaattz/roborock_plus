"""The blueprint reports what happened, at the right time.

The first version announced "已完成" as soon as the dispatch loop exited, which is
a few seconds after the clean *starts*. The household saw a completion notice
while the robot was still sweeping.

The completion notice now waits for the clean to actually end, and says which of
four things happened:

* the vacuum went offline or reported an error -- `critical`
* it never finished within the wait -- `warning`
* it came home -- `info`, with the elapsed time
* it stopped somewhere else -- `warning`, because that is a robot stuck mid-room

The four are expressed as nested `if`/`else` rather than `choose`: the repo's
blueprint validator walks every action node and `choose` branches carry
`conditions`/`then` instead of the keys it recognises.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "blueprints" / "扫地机分区定时清扫.yaml"


class _Loader(yaml.SafeLoader):
    pass


def _any(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_Loader.add_constructor(None, _any)


def load() -> dict:
    return yaml.load(BLUEPRINT.read_text(encoding="utf-8"), Loader=_Loader)


def completion_block() -> dict:
    """The top-level action that reports the outcome of a started clean.

    Identified by its wait: it is the only top-level action that waits on the
    task ending.
    """
    for action in load()["actions"]:
        steps = action.get("then") if isinstance(action, dict) else None
        if not steps:
            continue
        if any(isinstance(step, dict) and "wait_template" in step for step in steps):
            return action
    raise AssertionError("completion block not found")


def flat(node) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return " ".join(flat(v) for v in node.values())
    if isinstance(node, list):
        return " ".join(flat(v) for v in node)
    return str(node)


def levels(node, found=None) -> list[str]:
    """Every `level:` value under a node, in document order."""
    found = [] if found is None else found
    if isinstance(node, dict):
        if "level" in node:
            found.append(node["level"])
        for value in node.values():
            levels(value, found)
    elif isinstance(node, list):
        for item in node:
            levels(item, found)
    return found


def test_completion_waits_for_the_clean_to_end() -> None:
    """THE BUG: the old notice fired while the robot was still sweeping.

    The gate must be a `wait_template` on the task ending, not an `is_state` read
    taken when the dispatch loop finished.
    """
    body = completion_block()["then"]
    waits = [step for step in body if isinstance(step, dict) and "wait_template" in step]
    assert waits, "the completion block waits for the task to end"
    wait = waits[0]
    assert "not is_state(task_active_entity, 'on')" in flat(wait["wait_template"]), (
        "it must wait for the task to stop, not sample it once")
    assert wait["timeout"]["hours"] >= 4, (
        "a clean can run for hours; a short timeout would announce a false"
        " 'still running'")
    assert wait.get("continue_on_timeout") is True


def test_the_verdict_waits_for_the_robot_to_reach_the_dock() -> None:
    """Measured 2026-11-10 15:09:21: `task_active` went off while `vacuum` was
    still `returning`, and only at 15:09:26 did it read `docked`.

    The verdict used to be a single sample taken in that gap, so a finished
    clean was announced as "清扫异常结束". A second, bounded wait has to give
    the robot the seconds it needs to reach the dock before the outcome is
    decided -- while still ending in the failure branch when it never arrives.
    """
    body = completion_block()["then"]
    waits = [step for step in body if isinstance(step, dict) and "wait_template" in step]
    assert len(waits) >= 2, (
        "after the task ends the block must wait for the robot to dock before "
        "deciding the outcome")

    settle = waits[1]
    text = flat(settle["wait_template"])
    assert "docked" in text and "charging" in text, (
        "docking is what makes a clean a success")
    timeout = settle["timeout"]
    seconds = timeout.get("seconds", 0) if isinstance(timeout, dict) else 0
    assert 10 <= seconds <= 300, (
        "long enough to cover the measured 4-11 s gap, short enough that a robot "
        f"that stopped in the room is still reported promptly; got {timeout}")
    assert settle.get("continue_on_timeout") is True, (
        "a timeout is 'it did not come home', which the else branch must still see")


def test_the_four_outcomes_are_all_present() -> None:
    text = flat(completion_block())
    assert "清扫中断" in text, "offline or error"
    assert "清扫超时" in text, "never finished"
    assert "已完成" in text, "came home"
    assert "清扫异常结束" in text, "stopped away from the dock"


def test_each_outcome_carries_a_matching_severity() -> None:
    got = levels(completion_block())
    assert "critical" in got, "offline/error is the serious one"
    assert got.count("warning") >= 2, "timeout and stopped-elsewhere are warnings"
    assert "info" in got, "success is informational"


def test_the_finished_notice_reports_how_long_it_took() -> None:
    text = flat(completion_block())
    assert "clean_minutes" in text
    assert "用时" in text


def test_a_clean_that_stopped_away_from_the_dock_is_not_called_a_success() -> None:
    text = flat(completion_block())
    assert "没有回到基站" in text, (
        "a robot stopped mid-room must be distinguishable from finishing")


def test_the_still_running_case_is_reported_rather_than_dropped() -> None:
    """When the wait times out, silence would look like success."""
    text = flat(completion_block())
    assert "未结束" in text


def test_offline_detection_covers_unavailable_and_error() -> None:
    text = flat(completion_block())
    assert "unavailable" in text and "'error'" in text
