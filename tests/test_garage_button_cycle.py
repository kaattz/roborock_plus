"""Tests for the garage-door BLE button cycle.

The automation replaced a blueprint instance that used a 40s timer to guess the
door's motion while the hardware reports a real position and finishes in under a
second. That mismatch reopened the door 9.5s after the garage state machine
closed it, twice on 2026-10-08.

These pin the behaviour that replaced it, and in particular the property that
matters: an unknown position must never produce a close.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "garage_button_cycle"
    / "button_cycle.json"
)

DOOR = "cover.vacuum_garage_door"
BUTTON = "event.vacuum_garage_door_click"


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _logic_blob(config: dict) -> str:
    """Serialise only the parts that run.

    The description deliberately *names* the blueprint's timer to explain why it
    was replaced, so scanning the whole document for those words would fail on
    its own documentation.
    """
    return json.dumps(
        {
            "triggers": config["triggers"],
            "conditions": config["conditions"],
            "actions": config["actions"],
        },
        ensure_ascii=False,
    )


def _branches(config: dict) -> list:
    return config["actions"][0]["choose"]


class TestNoTimerAndNoHelpers:
    """The whole point is that a real position makes the timer unnecessary."""

    def test_no_helper_entities_are_referenced(self) -> None:
        blob = _logic_blob(_config())
        for helper in (
            "input_datetime.garage_door_moving_end_time",
            "input_select.garage_door_last_action",
        ):
            assert helper not in blob, (
                f"{helper} only existed to compensate for a missing position; "
                "using it would add a second, staler source of truth"
            )

    def test_no_duration_based_guessing(self) -> None:
        blob = _logic_blob(_config())
        assert "total_duration" not in blob
        assert "timedelta" not in blob, "no time arithmetic may decide the door"


class TestTriggerAndGate:
    def test_triggers_on_the_ble_button(self) -> None:
        config = _config()
        assert config["triggers"][0]["entity_id"] == BUTTON

    def test_only_a_single_click_acts(self) -> None:
        config = _config()
        condition = config["conditions"][0]
        assert condition["condition"] == "state"
        assert condition["attribute"] == "event_type"
        assert condition["state"] == "单击"

    def test_mode_is_single(self) -> None:
        """A press is discrete; overlapping runs must not both act."""
        assert _config()["mode"] == "single"


class TestPressSemantics:
    def test_a_moving_door_stops(self) -> None:
        branch = _branches(_config())[0]
        assert branch["conditions"][0]["state"] == ["opening", "closing"]
        assert branch["sequence"][0]["action"] == "cover.stop_cover"

    def test_a_closed_door_opens(self) -> None:
        branch = _branches(_config())[1]
        assert branch["conditions"][0]["condition"] == "numeric_state"
        assert "below" in branch["conditions"][0]
        assert branch["sequence"][0]["action"] == "cover.open_cover"

    def test_an_open_door_closes(self) -> None:
        branch = _branches(_config())[2]
        assert branch["conditions"][0]["condition"] == "numeric_state"
        assert "above" in branch["conditions"][0]
        assert branch["sequence"][0]["action"] == "cover.close_cover"

    def test_the_two_position_branches_do_not_overlap(self) -> None:
        """A position must map to exactly one action, never both."""
        branches = _branches(_config())
        below = branches[1]["conditions"][0]["below"]
        above = branches[2]["conditions"][0]["above"]
        assert above + 1 == below, (
            "the thresholds must be adjacent so every integer position lands in "
            "exactly one branch"
        )


class TestUnknownPositionIsRefused:
    """The one direction that can hit the robot must fail closed."""

    def test_a_default_branch_exists(self) -> None:
        block = _config()["actions"][0]
        assert "default" in block, (
            "without a default, a press with no position would pass silently"
        )

    def test_the_default_does_not_move_the_door(self) -> None:
        default = json.dumps(_config()["actions"][0]["default"], ensure_ascii=False)
        for forbidden in ("close_cover", "open_cover", "stop_cover"):
            assert forbidden not in default, (
                f"the unknown-position path must not call {forbidden}"
            )

    def test_the_default_tells_the_owner(self) -> None:
        default = json.dumps(_config()["actions"][0]["default"], ensure_ascii=False)
        assert "alert_notify" in default

    def test_no_branch_accepts_a_missing_position_implicitly(self) -> None:
        """
        A bare `below:`/`above:` on a missing attribute does not match, which is
        what makes the default reachable. Pinned so a future `or` cannot quietly
        widen the close branch.
        """
        for branch in _branches(_config()):
            blob = json.dumps(branch, ensure_ascii=False)
            assert '"condition": "or"' not in blob
            assert '"condition": "not"' not in blob
