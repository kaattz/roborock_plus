"""The auto-unlock must release the clutch on a *stopped* door.

Purpose (confirmed by the owner): once the door is fully shut, release the motor
clutch so the door can be pushed by hand -- it is a manual/electric hybrid.
`cover.stop_cover` does that on a door that has finished travelling. Issued while
the door is still moving it does the opposite: it parks the door part-way open.

Measured 2026-10-10: the trigger fired on the device's echo (position 0 within
0.8s of the close command), then a 5-second delay, then stop_cover landed about a
fifth of the way through a 35-second travel. The door came to rest at 77 and at 38
in two separate runs.

The judgement moved to the contact sensor on 2026-10-14. The defect these tests
guard is unchanged -- a PAUSE on a moving door -- but the signal that proves the
door stopped is now a physical contact rather than a position reading plus a
fixed wait. Measured that day:

    14:21:15.373  contact -> off     the door reached the closed position
    14:21:20.736  cover   -> closed  the cover reported it 5.4s later

    14:15:16.976  cover   -> open    echo: claimed "arrived" after 0.7s
    14:15:23.499  contact -> on      the contact only flips when the magnet parts

So the contact flips at the *end* of a travel and the cover is polluted from the
*start*, which is exactly why it can replace the wait.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
AUTOMATION = REPO / "automations" / "vacuum_garage_door_auto_unlock.yaml"
CONTACT = "binary_sensor.vacuum_garage_door_contact_state"


def _config() -> dict:
    return yaml.safe_load(AUTOMATION.read_text(encoding="utf-8"))


def _walk(node: object):
    """Yield every dict in a nested action tree."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def test_unique_id_is_preserved() -> None:
    """`id` is the HA unique_id; changing it orphans the entity's history."""
    assert _config()["id"] == "1788771256556"


def test_triggers_on_the_contact_leaving_the_closed_position() -> None:
    """The trigger must be the contact, not the echoed position.

    A position trigger is satisfied by the device's echo within a second of the
    command, which is what put the PAUSE mid-travel in the first place. The
    contact cannot be echoed -- nothing writes to it but the magnet.
    """
    triggers = _config()["triggers"]
    assert len(triggers) == 1, "one trigger, or the guards below only cover one"
    trigger = triggers[0]

    assert trigger["entity_id"] == CONTACT, (
        "the trigger must read the contact sensor; a current_position trigger "
        "fires on the device's echo of the command, which is the original defect"
    )
    assert trigger["trigger"] == "state"
    assert trigger["from"] == "on", "only a door that was open can close"
    assert trigger["to"] == "off", "the contact reads off at the closed position"


def test_the_trigger_requires_a_sustained_change() -> None:
    """A reed switch bounces, and the motor may still be turning.

    Magnet contact is made slightly before the door reaches the end of its
    travel, so acting on the instant of the change can still catch a moving
    door -- the same mistake in a new place.
    """
    trigger = _config()["triggers"][0]
    assert trigger.get("for"), (
        "the trigger must hold for a moment before firing; without it a bouncing "
        "reed or the last fraction of travel can put the PAUSE on a moving door"
    )
    seconds = trigger["for"]["seconds"]
    assert seconds >= 5, f"a {seconds}s hold is too short to settle the door"


def test_unavailable_is_refused_rather_than_assumed() -> None:
    """`unavailable` must not fall into either boolean branch.

    Releasing the clutch on a door that is actually open is the dangerous
    direction: a heavy door can fall. The sensor is BLE and this house's other
    contact sensors drop out periodically, so the state is reachable in practice.
    """
    config = _config()
    text = json.dumps(config, ensure_ascii=False)

    # The guard must be an explicit equality against 'off', never `is_state(...)`
    # treated loosely, and never a check that merely asserts "not on".
    guards = [
        step
        for step in _walk(config["actions"])
        if step.get("condition") == "state" and step.get("entity_id") == CONTACT
    ]
    assert guards, "the stop must be guarded by the contact's current state"
    assert all(g["state"] == "off" for g in guards), (
        "the guard must require the contact to read exactly 'off'; anything "
        "looser lets unavailable reach the stop"
    )

    # And the stop itself has to live in the `then` branch, so the else path
    # cannot reach it.
    assert "cover.stop_cover" in text
    stops = [
        step
        for step in _walk(config["actions"])
        if "cover.stop_cover" in json.dumps(step.get("then", []), ensure_ascii=False)
    ]
    assert stops, "stop_cover must sit in a then branch, not as a bare action"


def test_the_stop_is_not_unconditional() -> None:
    """A bare stop_cover would fire even when the door never shut."""
    for step in _config()["actions"]:
        assert step.get("action") != "cover.stop_cover", (
            "stop_cover must sit behind the contact check; an unconditional stop "
            "is the original defect in a new place"
        )


def test_failure_paths_alert() -> None:
    """When the clutch is not released the door cannot be pushed by hand.

    That is invisible to anyone not standing at the door, so it must be said out
    loud rather than failing silently.
    """
    # Exact match, deliberately. A substring test for "alert_notify" also passes
    # for `script.alert_notify_something_else`, and the mutation check caught
    # that: renaming the action made the notification disappear while this test
    # stayed green. The action name is the thing being asserted.
    alerts = [
        step
        for step in _walk(_config()["actions"])
        if step.get("action") == "script.alert_notify"
    ]
    assert alerts, "the refusal path must notify; silence looks like a locked door"
