"""Tests for the garage-door state machine's trigger guards.

The full-chain run on 2026-10-08 19:41 exercised the automation against real
hardware for the first time, and it immediately exposed a defect that no
single-component test could see: the departure trigger fired on `to: on` with no
`from:`, so it fired on every *recovery* of the danger-zone sensor, not only on
a real departure.

The sensor alternates `on -> unknown -> on` while cleaning, because a sample is
trusted for `2 x 5s = 10s` while a map read takes a measured median of 6s and
often 11s. Each recovery was a fresh edge, so one departure became four and the
robot was paused mid-clean three times (19:42:00, 19:42:38, 19:43:21).

These tests read the built config and assert the guards that prevent it,
including the ones that must survive future edits.
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "garage_state_machine"
    / "state_machine.json"
)

VACUUM = "vacuum.g20s_ultra"
STATUS = "sensor.g20s_ultra_status"
COVER = "cover.vacuum_garage_door"
TASK_ACTIVE = "binary_sensor.g20s_ultra_task_active"
CLEAR_OF_GARAGE = "binary_sensor.g20s_ultra_clear_of_garage"


def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _trigger(config: dict, trigger_id: str) -> dict:
    for trigger in config["triggers"]:
        if trigger.get("id") == trigger_id:
            return trigger
    raise AssertionError(f"trigger {trigger_id!r} missing from the config")


def _branch(config: dict, index: int) -> dict:
    return config["actions"][0]["choose"][index]


class TestDepartureTriggerRequiresARealDeparture:
    """The defect the full-chain run found."""

    def test_leave_trigger_requires_from_off(self) -> None:
        """Without `from:`, a flap recovery is indistinguishable from a departure."""
        trigger = _trigger(_config(), "leave")
        assert trigger["from"] == "off", (
            "the departure trigger must require `from: off`; otherwise the "
            "sensor's on -> unknown -> on flapping re-runs phase B and pauses "
            "the robot mid-clean"
        )
        assert trigger["to"] == "on"

    def test_leave_trigger_is_keyed_on_the_danger_zone_sensor(self) -> None:
        """`in_safe_zone` is `on` at the dock, so it cannot mean "departed"."""
        trigger = _trigger(_config(), "leave")
        assert trigger["entity_id"] == CLEAR_OF_GARAGE

    def test_departure_is_not_keyed_on_the_inverted_sensor(self) -> None:
        config = _config()
        blob = json.dumps(config, ensure_ascii=False)
        assert "binary_sensor.g20s_ultra_in_safe_zone" not in blob


class TestReturnTriggerIsSustained:
    """A 2.02s status blip must not open the door."""

    def test_return_trigger_has_a_duration(self) -> None:
        trigger = _trigger(_config(), "return")
        assert trigger.get("for", {}).get("seconds", 0) >= 5, (
            "the robot reported `returning` for 2.02s while docked on "
            "2026-10-08 01:26; a sustained requirement filters that"
        )


class TestLeavingRequiresTheRobotToBeAway:
    def test_phase_b_requires_a_cleaning_status(self) -> None:
        branch = _branch(_config(), 0)
        assert any(
            condition.get("entity_id") == STATUS for condition in branch["conditions"]
        )

    def test_phase_b_requires_the_robot_not_to_be_parked(self) -> None:
        branch = _branch(_config(), 0)
        blob = json.dumps(branch["conditions"], ensure_ascii=False)
        assert VACUUM in blob, "a docked robot must not count as 'having left'"

    def test_phase_b_requires_the_door_to_be_open(self) -> None:
        """Makes a repeated edge harmless: no second pause behind a shut door."""
        branch = _branch(_config(), 0)
        assert any(
            condition.get("condition") == "numeric_state"
            and condition.get("entity_id") == COVER
            and condition.get("above") == 5
            for condition in branch["conditions"]
        )


class TestParkingRequiresConfirmationTheRobotIsInside:
    """The sensor must agree the robot is in the danger zone before it shuts."""

    def test_phase_d_checks_the_danger_zone_inside_an_if(self) -> None:
        branch = _branch(_config(), 2)
        serialised = json.dumps(branch, ensure_ascii=False)
        assert CLEAR_OF_GARAGE in serialised, (
            "a stale `clear` reading must not be able to close the door onto "
            "the robot"
        )

    def test_a_non_confirmation_alerts_rather_than_going_silent(self) -> None:
        branch = _branch(_config(), 2)
        block = _branch(_config(), 2)["sequence"][-1]
        assert "else" in block, (
            "when the sensor does not confirm the robot is inside, the door "
            "must stay open AND the owner must be told"
        )
        assert "alert_notify" in json.dumps(block["else"], ensure_ascii=False)

    def test_phase_d_settles_before_closing(self) -> None:
        """Mop attach/detach blips are shorter than the delay."""
        branch = _branch(_config(), 2)
        assert branch["sequence"][0].get("delay") == "00:00:08"


class TestTheAutomationNeverClosesOnATimeout:
    """No wait may be treated as success, and no window may end in a close."""

    def test_every_wait_continues_on_timeout(self) -> None:
        config = _config()
        blob = json.dumps(config, ensure_ascii=False)
        waits = 0
        found_true = 0

        def walk(node: object) -> None:
            nonlocal waits, found_true
            if isinstance(node, dict):
                if "wait_template" in node:
                    waits += 1
                    if node.get("continue_on_timeout") is True:
                        found_true += 1
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(config)
        assert waits > 0, "expected the config to wait on the door"
        assert waits == found_true, (
            "every wait must set continue_on_timeout so a stalled door is "
            "reported instead of silently treated as done"
        )
        assert blob.count("alert_notify") >= 5, "failures must reach the owner"

    def test_timeouts_never_close_the_door(self) -> None:
        """A timeout path alerts and stops; it must not act on the door."""
        config = _config()
        blob = json.dumps(config, ensure_ascii=False)
        assert '"stop"' in blob

        def closing_after_alert(node: object, seen_alert: bool = False) -> bool:
            """Whether a close follows an alert in the same sequence.

            Actions are matched as substrings: the config's action is
            `script.alert_notify`, and an exact comparison with `alert_notify`
            never matched -- so this check passed vacuously and a mutation that
            appended a close to every alert path escaped.
            """
            if isinstance(node, dict):
                action = node.get("action")
                if isinstance(action, str) and "alert_notify" in action:
                    seen_alert = True
                if seen_alert and action == "cover.close_cover":
                    return True
                return any(
                    closing_after_alert(value, seen_alert) for value in node.values()
                )
            if isinstance(node, list):
                return any(closing_after_alert(item, seen_alert) for item in node)
            return False

        for index in range(3):
            branch = _branch(config, index)
            assert not closing_after_alert(branch), (
                f"branch {index} closes the door after alerting, which means a "
                "failure path still moves the door"
            )


class TestClockDiscipline:
    def test_automation_is_queued_with_a_bound(self) -> None:
        """Overlapping runs must queue, but not without limit."""
        config = _config()
        assert config["mode"] == "queued"
        assert config["max"] >= 2

    def test_the_door_is_only_ever_moved_by_a_named_action(self) -> None:
        config = _config()
        blob = json.dumps(config, ensure_ascii=False)
        # No template may decide which service runs.
        assert "service: {{" not in blob
        assert '"action": "{{' not in blob
