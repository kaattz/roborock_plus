"""Tests for the garage-door BLE button cycle.

History, because it explains what these guard:

The blueprint this replaced used a 40s timer to guess whether the door was moving.
The door reports a real position and finishes in ~34s, and the blueprint subscribed
to every cover service call, so it could not tell the state machine's command from
a button press. That reopened the door 9.5s after the state machine closed it,
twice on 2026-10-08.

The first native replacement read the cover's `opening`/`closing` state instead.
That is also broken on this device: it has no status property, so those are
optimistic flags the integration clears as soon as the echoed position changes
(~0.5s). Measured over 7 days they last a mean of 0.79s (max 1.71s, n=20) out of a
~34s travel, so the stop branch was unreachable and a press mid-travel reversed the
door. A press while *opening* therefore called `close_cover` -- the one direction
that can hit the robot in the doorway. It was `off` throughout, which is the only
reason that never happened.

These pin the replacement: "moving" comes from time since the last command, and
open/close comes from the contact sensor at the closed position.
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
CONTACT = "binary_sensor.vacuum_garage_door_contact_state"
MOVING_END = "input_datetime.garage_door_moving_end_time"


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _logic_blob(config: dict) -> str:
    """Serialise only the parts that run.

    The description deliberately *names* the retired blueprint's timer and the
    `opening`/`closing` defect to explain the history, so scanning the whole
    document for those words would fail on its own documentation.
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


def _press_branches(config: dict) -> list:
    """The inner choose of the press branch."""
    return _branches(config)[1]["sequence"][1]["choose"]


def _record_branch(config: dict) -> dict:
    return _branches(config)[0]


class TestMovingIsJudgedByTimeNotCoverState:
    """The shipped defect: `opening`/`closing` cannot answer "is it moving"."""

    def test_the_stop_branch_does_not_use_the_cover_state(self) -> None:
        stop = _press_branches(_config())[1]
        blob = json.dumps(stop, ensure_ascii=False)
        for flag in ("opening", "closing"):
            assert flag not in blob, (
                f"the stop branch reads the cover's {flag!r} state, which this "
                "device clears within ~0.5s of a command; the branch would be "
                "unreachable and a press mid-travel would reverse the door"
            )

    def test_the_stop_branch_reads_the_travel_clock(self) -> None:
        """And reads it as epoch seconds, never as a datetime.

        `as_datetime()` returns a naive datetime for a value with no offset while
        `now()` is timezone-aware, so `now() < as_datetime(state)` raises
        TypeError at runtime. An exception in a condition aborts the run, so the
        stop branch would never fire and the button would stop responding -- the
        defect back, with the fix apparently in place.
        """
        stop = _press_branches(_config())[1]
        condition = stop["conditions"][0]
        assert condition["condition"] == "template"
        template = condition["value_template"]
        assert "now().timestamp()" in template
        assert "as_datetime" not in template, (
            "comparing now() to as_datetime(...) mixes an aware and a naive "
            "datetime and raises at runtime"
        )
        assert "state_attr" in template and "timestamp" in template

    def test_something_records_the_travel_clock(self) -> None:
        """Without a recorder the clock never advances and nothing is 'moving'."""
        ids = {t.get("id") for t in _config()["triggers"]}
        assert "command" in ids, (
            "no trigger records when travel should end; the stop branch would "
            "never fire"
        )

    def test_the_recorder_listens_to_cover_service_calls(self) -> None:
        command = next(t for t in _config()["triggers"] if t.get("id") == "command")
        assert command["trigger"] == "event"
        assert command["event_type"] == "call_service"
        assert command["event_data"] == {"domain": "cover"}

    def test_the_recorder_filters_to_this_door(self) -> None:
        """Another cover moving must not mark this door as travelling."""
        conditions = _record_branch(_config())["conditions"]
        blob = json.dumps(conditions, ensure_ascii=False)
        assert DOOR in blob, (
            "the recorder does not filter by entity, so any curtain in the house "
            "would stop the garage door on the next press"
        )

    def test_the_recorder_only_cares_about_moving_commands(self) -> None:
        """A stray cover service must not start a travel window."""
        conditions = _record_branch(_config())["conditions"]
        blob = json.dumps(conditions, ensure_ascii=False)
        for service in ("open_cover", "close_cover", "stop_cover"):
            assert service in blob, f"{service} is not recognised by the recorder"

    def test_the_recorder_writes_the_moving_end_helper(self) -> None:
        sequence = _record_branch(_config())["sequence"]
        assert sequence[0]["action"] == "input_datetime.set_datetime"
        assert sequence[0]["target"]["entity_id"] == MOVING_END


class TestModeAllowsTheSelfTriggeredRecording:
    """`single` would drop the recording run, restoring the original defect."""

    def test_mode_is_queued(self) -> None:
        assert _config()["mode"] == "queued", (
            "the automation's own cover command fires the recording trigger; "
            "under `single` that re-entry is dropped as a duplicate, the travel "
            "window is never recorded, and a press mid-travel reverses the door"
        )

    def test_it_is_not_armed_by_a_config_reload(self) -> None:
        assert _config().get("initial_state") is False


class TestTriggerAndGate:
    def test_triggers_on_the_ble_button(self) -> None:
        ids = {t.get("id") for t in _config()["triggers"]}
        assert "press" in ids

    def test_only_a_single_click_acts(self) -> None:
        press = _branches(_config())[1]
        assert any(
            c.get("condition") == "state"
            and c.get("entity_id") == BUTTON
            and c.get("attribute") == "event_type"
            and c.get("state") == "单击"
            for c in press["conditions"]
        ), "a double-click must not be treated as a press"


class TestPressSemantics:
    def test_a_moving_door_stops(self) -> None:
        stop = _press_branches(_config())[1]
        assert stop["sequence"][0]["action"] == "cover.stop_cover"

    def test_a_shut_door_opens(self) -> None:
        """The contact reads `off` at the closed position."""
        branch = _press_branches(_config())[2]
        assert branch["conditions"][0]["condition"] == "state"
        assert branch["conditions"][0]["entity_id"] == CONTACT
        assert branch["conditions"][0]["state"] == "off"
        assert branch["sequence"][0]["action"] == "cover.open_cover"

    def test_an_unshut_door_closes(self) -> None:
        branch = _press_branches(_config())[3]
        assert branch["conditions"][0]["condition"] == "state"
        assert branch["conditions"][0]["entity_id"] == CONTACT
        assert branch["conditions"][0]["state"] == "on"
        assert branch["sequence"][0]["action"] == "cover.close_cover"

    def test_the_stop_branch_is_checked_before_the_contact_branches(self) -> None:
        """Order is load-bearing: a travelling door must not be reversed."""
        branches = _press_branches(_config())
        stop = json.dumps(branches[1], ensure_ascii=False)
        assert "stop_cover" in stop
        assert "now().timestamp()" in stop, "the stop branch must read the clock"
        for later in branches[2:]:
            assert "stop_cover" not in json.dumps(later, ensure_ascii=False)

    def test_the_refusal_is_checked_before_everything(self) -> None:
        """Both readings must be usable before any door command is considered.

        The branch reads the local `travel_end` / `contact` variables, which the
        press branch binds from the entities just above it -- so this asserts on
        those names and separately confirms where they come from.
        """
        press = _branches(_config())[1]
        bindings = json.dumps(press["sequence"][0], ensure_ascii=False)
        assert MOVING_END in bindings, "travel_end must be bound from the helper"
        assert CONTACT in bindings, "contact must be bound from the sensor"

        first = _press_branches(_config())[0]
        blob = json.dumps(first, ensure_ascii=False)
        assert "travel_end" in blob, "an unknown clock must be refused"
        assert "contact" in blob, "an unknown contact must be refused"
        assert "alert_notify" in blob
        for forbidden in ("stop_cover", "open_cover", "close_cover"):
            assert forbidden not in blob, (
                f"the refusal branch must not reach {forbidden}"
            )


class TestUnknownReadingsAreRefused:
    """The one direction that can hit the robot must fail closed."""

    def test_the_guard_requires_both_readings(self) -> None:
        guard = _press_branches(_config())[0]["conditions"][0]
        template = guard["value_template"]
        assert "travel_end" in template, "an unknown clock must be refused"
        assert "contact" in template, "an unknown contact must be refused"

    def test_a_default_branch_exists(self) -> None:
        block = _branches(_config())[1]["sequence"][1]
        assert "default" in block, (
            "without a default, a state neither branch claims would pass silently"
        )

    def test_the_default_does_not_move_the_door(self) -> None:
        default = json.dumps(
            _branches(_config())[1]["sequence"][1]["default"], ensure_ascii=False
        )
        for forbidden in ("close_cover", "open_cover", "stop_cover"):
            assert forbidden not in default

    def test_both_refusal_paths_tell_the_owner(self) -> None:
        first = json.dumps(_press_branches(_config())[0], ensure_ascii=False)
        default = json.dumps(
            _branches(_config())[1]["sequence"][1]["default"], ensure_ascii=False
        )
        assert "alert_notify" in first
        assert "alert_notify" in default

    def test_no_branch_widens_a_condition_with_or_or_not(self) -> None:
        """A negated test would accept `unavailable`."""
        for branch in _press_branches(_config()):
            blob = json.dumps(branch, ensure_ascii=False)
            assert '"condition": "or"' not in blob
            assert '"condition": "not"' not in blob


class TestNoReturnOfTheRetiredTimer:
    """The blueprint's compensating helpers must not creep back."""

    def test_the_last_action_helper_is_not_referenced(self) -> None:
        blob = _logic_blob(_config())
        assert "garage_door_last_action" not in blob, (
            "the contact answers 'which way is it' directly; a last-action helper "
            "would be a second, staler source of truth"
        )

    def test_no_duration_based_guessing(self) -> None:
        blob = _logic_blob(_config())
        assert "total_duration" not in blob
