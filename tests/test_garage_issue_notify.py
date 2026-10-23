"""The takeover notification must inform and never act.

Two real clean runs were refused for 33 minutes because a ``manual_hold`` latched
silently (2026-10-22 16:08 and 2026-10-23 14:00). The integration already fires
``roborock_plus_garage_issue``; nothing in the household consumed it. This
automation is that consumer, and it is deliberately dumb: report, do not act.

The safety-relevant part of this file is what the automation must NOT do. An
auto-reset would reward moving the door during a task -- the exact behaviour the
2026-10-23 narrowing exists to stop -- so every read-only assertion below is a
test about restraint, not about features.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
AUTOMATION = REPO / "automations" / "roborock_garage_issue_notify.yaml"
EVENT = "roborock_plus_garage_issue"
FORBIDDEN_PREFIXES = ("cover.", "vacuum.", "button.", "input_button.", "script.reset")
FORBIDDEN_EXACT = (
    "roborock_plus.reset_garage_control",
    "roborock_plus.request_garage_close",
    "roborock_plus.request_garage_open",
    "automation.trigger",
    "homeassistant.restart",
)


def _config() -> dict:
    return yaml.safe_load(AUTOMATION.read_text(encoding="utf-8"))


def _walk(node: object):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _actions() -> list[dict]:
    return [node for node in _walk(_config()["actions"]) if "action" in node or "service" in node]


def _called() -> list[str]:
    return [node.get("action") or node.get("service") for node in _actions()]


def test_unique_id_is_registered_and_stable() -> None:
    assert _config()["id"] == "1791600000001"


def test_it_triggers_only_on_the_garage_issue_event() -> None:
    triggers = _config()["triggers"]
    assert len(triggers) == 1
    assert triggers[0]["trigger"] == "event"
    assert triggers[0]["event_type"] == EVENT


def test_it_never_moves_anything() -> None:
    """Restraint is the point: this automation reports a stuck interlock."""
    called = _called()
    for name in called:
        assert not name.startswith(FORBIDDEN_PREFIXES), (
            f"{name} acts on the physical system; this notification must only report"
        )
        assert name not in FORBIDDEN_EXACT, (
            f"{name} is an action this read-only notification must not take"
        )


def test_it_never_resets_the_interlock() -> None:
    """Auto-reset would reward moving the door during a task.

    Reset is the human's decision precisely so a stuck interlock cannot be
    silently cleared and re-armed while somebody is standing in the doorway.
    Note the message *names* the service so the user knows what to do; what must
    never happen is the automation calling it.
    """
    called = _called()
    assert not any("reset_garage_control" in name for name in called)
    assert "roborock_plus.reset_garage_control" not in called


def test_it_notifies_through_the_household_script() -> None:
    called = _called()
    assert "script.alert_notify" in called
    notify = [
        node for node in _actions()
        if (node.get("action") or node.get("service")) == "script.alert_notify"
    ]
    assert len(notify) == 1
    assert notify[0]["data"]["level"] == "warning"


def test_the_message_carries_every_field_needed_to_understand_the_hold() -> None:
    blob = yaml.safe_dump(_config(), allow_unicode=True)
    for field in ("reason", "phase", "previous_phase", "cover_entity_id", "vacuum_entity_id"):
        assert f"trigger.event.data.{field}" in blob, (
            f"the alert must quote {field}; without it the user cannot tell an idle "
            f"takeover from a task-time one"
        )


def test_the_message_says_it_will_not_act_on_its_own() -> None:
    """The alert is the only chance to tell the user the interlock is latched."""
    blob = yaml.safe_dump(_config(), allow_unicode=True)
    assert "reset_garage_control" in blob  # named as the ACTION THE USER must take
    assert "不会自动解除" in blob


def test_a_cleared_issue_is_silent() -> None:
    """active=false is the user's own reset; re-notifying it is noise."""
    config = _config()
    branch = config["actions"][0]
    assert branch["if"][0]["condition"] == "template"
    assert "active" in branch["if"][0]["value_template"]
    assert branch["else"] in ([], None), "the inactive branch must do nothing"


def test_the_event_is_actually_published_by_the_integration() -> None:
    """A notification on an event nobody fires is decoration."""
    runtime = REPO / "custom_components/roborock_plus/garage_runtime.py"
    text = runtime.read_text(encoding="utf-8")
    assert "EVENT_GARAGE_ISSUE = 'roborock_plus_garage_issue'" in text
    assert "'previous_phase'" in text, (
        "the alert quotes previous_phase, so the event must carry it"
    )
