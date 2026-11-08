"""The cleaning blueprint gates on doors and reports every outcome.

Two additions to the blueprint, both from the household:

1. **Door sensors.** 主卧 and 次卧 have their own room doors. If the door is shut
   the robot cannot get in, so the run must not start. The door magnet's polarity
   differs per install -- the garage reports `off` when open -- so the parameter
   carries both the entities and which state counts as open.

2. **Notifications on every outcome.** Until now the blueprint spoke only when
   the retry window ran out. Pressing "run" by hand and getting nothing at all is
   indistinguishable from a broken automation: the reason (someone in the room, a
   door shut, the vacuum busy) was invisible.

The retry window is shared: people, doors and a busy vacuum are all "not yet", and
the same loop retries them. That matters because a door may open ten minutes after
the trigger, and the run should then start rather than wait for tomorrow.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "blueprints" / "扫地机分区定时清扫.yaml"


class _Loader(yaml.SafeLoader):
    """`!input` is a Home Assistant tag; treat it as the scalar it wraps."""


def _any(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_Loader.add_constructor(None, _any)


def load() -> dict:
    return yaml.load(BLUEPRINT.read_text(encoding="utf-8"), Loader=_Loader)


def flat(value) -> str:
    """All text of a nested structure, for substring assertions."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(flat(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(flat(v) for v in value)
    return str(value)


# --- the inputs exist --------------------------------------------------------

def test_the_blueprint_offers_door_sensors_and_their_polarity():
    inputs = load()["blueprint"]["input"]
    doors = inputs["doors"]["input"]
    assert "door_sensors" in doors
    assert "door_open_state" in doors, (
        "polarity has to be configurable: the garage door magnet reports `off` "
        "when fully open, so a hard-coded `on` would read an open door as shut")
    options = doors["door_open_state"]["selector"]["select"]["options"]
    assert options == ["on", "off"]


def test_the_door_sensor_picker_offers_doors_not_motion():
    selector = load()["blueprint"]["input"]["doors"]["input"]["door_sensors"]["selector"]
    # Home Assistant puts `device_class` beside `filter`, not inside it.
    classes = set(selector["entity"].get("device_class", []))
    assert {"door", "opening", "garage_door"} <= classes, classes
    assert "motion" not in classes, "a motion sensor is not a door"
    domains = {
        domain
        for entry in selector["entity"]["filter"]
        for domain in entry.get("domain", [])
    }
    assert domains == {"binary_sensor"}, domains


def test_every_outcome_has_a_notification_switch():
    notify = load()["blueprint"]["input"]["notify"]["input"]
    for key in ("notify_script", "notify_on_start", "notify_on_skip"):
        assert key in notify, key
    assert notify["notify_script"]["default"] == "script.alert_notify"


def test_the_retry_window_covers_hours_of_waiting():
    retry = load()["blueprint"]["input"]["retry"]["input"]
    window = retry["retry_window_minutes"]
    assert window["default"] == 30
    assert window["selector"]["number"]["max"] >= 480, (
        "a room door may stay shut for most of a day; the ceiling has to allow it")
    assert "人体" in flat(window) and "门" in flat(window), (
        "the description must say people and doors share this one window")


# --- the gate ---------------------------------------------------------------

def test_the_run_is_gated_on_doors_as_well_as_people_and_busyness():
    body = flat(load()["actions"])
    assert "doors_shut | count == 0" in body, (
        "the start condition must require every door to be open")


def test_the_door_check_uses_the_configured_polarity():
    body = flat(load()["actions"])
    assert "door_open_state" in body, "the polarity parameter must actually be used"
    assert "rejectattr('state', 'eq', open_state)" in body, (
        "a door counts as shut when its state is not the configured open state")


def test_an_empty_door_list_imposes_no_constraint():
    """Default is empty: rooms without a door of their own keep working."""
    doors = load()["blueprint"]["input"]["doors"]["input"]["door_sensors"]
    assert doors["default"] == []


# --- the notifications ------------------------------------------------------

def test_a_skipped_round_is_reported_and_says_why():
    body = flat(load()["actions"])
    assert "本轮跳过" in body
    for reason in ("区域内有人", "门未开", "扫地机正忙"):
        assert reason in body, f"a skip caused by {reason} must name it"


def test_starting_is_reported():
    assert "已开始清扫" in flat(load()["actions"])


def test_finishing_is_reported():
    assert "已完成" in flat(load()["actions"])


def test_the_skip_notice_is_not_inside_the_start_branch():
    """The bug this guards: a skip notice that only fires when nothing is skipped.

    The notification has to sit in the `else` of the start condition. Putting it
    inside `then` would announce "skipped" precisely when the run is starting.
    """
    actions = load()["actions"]
    repeat = next(a["repeat"] for a in actions if "repeat" in a)
    # sequence[0] is the variable block; sequence[1] is the guarded run.
    step = repeat["sequence"][1]
    assert "if" in step and "then" in step
    branch = next(item for item in step["then"] if isinstance(item, dict) and "if" in item and "else" in item)
    assert "if" in branch and "then" in branch and "else" in branch, (
        "the skip branch must be an `else`, not a sibling of the start actions")
    then_text = flat(branch["then"])
    else_text = flat(branch["else"])
    assert "已开始清扫" in then_text
    assert "本轮跳过" in else_text
    assert "本轮跳过" not in then_text


def test_the_final_failure_notice_mentions_a_shut_door():
    actions = load()["actions"]
    last = flat(actions[-1])
    assert "未执行" in last
    assert "门未开" in last
