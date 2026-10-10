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
CLEAR_OF_GARAGE = "binary_sensor.g20s_ultra_outside_danger_zone"


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
        """`in_danger_zone` is `on` at the dock, so it cannot mean "departed"."""
        trigger = _trigger(_config(), "leave")
        assert trigger["entity_id"] == CLEAR_OF_GARAGE

    def test_departure_is_not_keyed_on_the_inverted_sensor(self) -> None:
        config = _config()
        blob = json.dumps(config, ensure_ascii=False)
        assert "binary_sensor.g20s_ultra_in_danger_zone" not in blob


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


class TestParkingRequiresTheTaskToBeOver:
    """A dock visit mid-task must not be mistaken for a finished clean.

    `binary_sensor.g20s_ultra_task_active` cannot answer this on its own: its
    state list treats `charging`, `attaching_the_mop`, `detaching_the_mop` and
    `back_to_dock_washing_duster` as "no task". A robot that comes back to wash
    its mop or take on charge therefore reads as idle while the clean is still
    unfinished. Closing there would shut the door on a robot that is about to
    come back out, and phase C would not reopen it, because a robot going
    straight from `charging` to `cleaning` never reports `returning_home`.
    """

    END_MARKER = "sensor.sao_di_ji_v2_timestamp_2"

    def test_phase_d_consults_the_devices_end_of_clean_marker(self) -> None:
        branch = _branch(_config(), 2)
        serialised = json.dumps(branch, ensure_ascii=False)
        assert self.END_MARKER in serialised, (
            "phase D must distinguish a finished clean from a mid-task dock "
            "visit; task_active alone cannot"
        )

    def test_the_marker_is_required_in_both_places(self) -> None:
        """Once in the conditions, once after the settle delay re-check."""
        branch = _branch(_config(), 2)
        in_conditions = any(
            self.END_MARKER in json.dumps(c, ensure_ascii=False)
            for c in branch["conditions"]
        )
        in_sequence = any(
            self.END_MARKER in json.dumps(s, ensure_ascii=False)
            for s in branch["sequence"]
        )
        assert in_conditions, "the guard must gate the branch"
        assert in_sequence, (
            "the guard must be re-checked after the delay, or a task that "
            "restarts inside those 8 seconds would still close the door"
        )

    def test_an_unavailable_marker_refuses_rather_than_guesses(self) -> None:
        """A missing timestamp must not be treated as 'just finished'."""
        branch = _branch(_config(), 2)
        guard = next(
            c for c in branch["conditions"]
            if self.END_MARKER in json.dumps(c, ensure_ascii=False)
        )
        template = guard["value_template"]
        assert "false" in template, (
            "the template must render false when the marker is unknown, "
            "unavailable or empty"
        )
        for missing in ("unknown", "unavailable", "none"):
            assert missing in template, f"{missing} must be handled"

    def test_the_window_is_short_enough_to_exclude_a_previous_task(self) -> None:
        """A mid-task visit leaves the marker at the previous task's time."""
        branch = _branch(_config(), 2)
        guard = next(
            c for c in branch["conditions"]
            if self.END_MARKER in json.dumps(c, ensure_ascii=False)
        )
        import re

        limit = int(re.search(r"< (\d+)", guard["value_template"]).group(1))
        assert limit <= 30 * 60, (
            "the window must be well under the gap between separate tasks; a "
            "multi-hour window would accept yesterday's finish"
        )
        assert limit >= 2 * 60, (
            "the window must outlast the trip from the last room to the dock"
        )


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


def _delay_seconds(delay: object) -> int:
    """Read a HA delay, which may be 'HH:MM:SS' or {seconds: N}."""
    if isinstance(delay, dict):
        return int(delay["seconds"])
    hours, minutes, seconds = (int(part) for part in str(delay).split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _echo_settle_seconds() -> int:
    """Load the shared timing constant by path.

    `custom_components.roborock_plus` cannot be imported here: its
    `__init__.py` pulls in the `roborock` library, which is not installed in the
    test environment. Loading the module file directly avoids that.
    """
    import importlib.util

    path = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "roborock_plus"
        / "door_timing.py"
    )
    spec = importlib.util.spec_from_file_location("door_timing_for_state_machine", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ECHO_SETTLE_SECONDS


def _door_waits() -> list[tuple[str, dict, object]]:
    """Return (path, wait_step, preceding_step) for every door-position wait.

    Recursive, because the parking branch keeps its door wait inside `if/then`.
    `preceding_step` is the sibling immediately before the wait in the same
    list, which is where the settle delay must be inserted.

    Verified against the pre-fix config: finds 3 waits
    (`choose[0].sequence[4]`, `choose[1].sequence[1]`,
    `choose[2].sequence[5].then[1]`), none of which has a delay yet.
    """
    config = _config()
    found: list[tuple[str, dict, object]] = []

    def visit_list(items: list, path: str) -> None:
        for index, item in enumerate(items):
            child = f"{path}[{index}]"
            if (
                isinstance(item, dict)
                and "wait_template" in item
                and "current_position" in item["wait_template"]
            ):
                found.append((child, item, items[index - 1] if index else None))
            visit(item, child)

    def visit(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, list):
                    visit_list(value, f"{path}.{key}")
                elif isinstance(value, dict):
                    visit(value, f"{path}.{key}")
        elif isinstance(node, list):
            visit_list(node, path)

    visit(config["actions"][0]["choose"], "choose")
    return found


class TestDoorWaitsOutlastTheEcho:
    """A wait that is satisfied by the command echo proves nothing.

    The device writes the target position into `current-position` within half a
    second of any motor command, while the door is still at the old position. A
    wait keyed only on position therefore passes instantly, and the anti-crush
    waits in the closing phases never actually hold.
    """

    def test_all_three_door_waits_are_found(self) -> None:
        """Guard the scan itself: a structural change must not silently skip one."""
        paths = [path for path, _, _ in _door_waits()]
        assert len(paths) == 3, (
            f"expected 3 door-position waits, found {len(paths)}: {paths}. "
            "If the automation legitimately changed, update this count -- but "
            "check you are not just failing to see a nested wait."
        )

    def test_the_nested_parking_wait_is_included(self) -> None:
        """The regression this test exists for: branch 2's wait is inside if/then."""
        paths = [path for path, _, _ in _door_waits()]
        assert any("choose[2]" in path for path in paths), (
            "the parking branch's door wait is nested in if/then and was not "
            f"found by the scan; found only {paths}"
        )

    def test_every_door_wait_has_a_settle_delay_before_it(self) -> None:
        for path, _, previous in _door_waits():
            assert previous is not None, f"{path} has no preceding step"
            assert "delay" in previous, (
                f"{path} reads current_position with no settle delay in front of "
                "it, so the device's echo of the command satisfies it"
            )

    def test_the_settle_delay_is_at_least_the_echo_window(self) -> None:
        settle = _echo_settle_seconds()
        for path, _, previous in _door_waits():
            seconds = _delay_seconds(previous["delay"])
            assert seconds >= settle, (
                f"{path} waits only {seconds}s before reading position; the echo "
                f"window is {settle}s"
            )

    def test_the_paused_wait_is_not_given_a_door_delay(self) -> None:
        """The `paused` wait is about the vacuum, not the door."""
        for _, step, _ in _door_waits():
            assert "is_state" not in step["wait_template"], (
                "a vacuum-state wait was misidentified as a door wait"
            )
