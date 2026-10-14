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
# The physical switch at the closed position. It answers "is the door shut" and
# nothing else: it flips `on` as soon as the door leaves the closed position,
# 7.2s into a 34s travel, so it says nothing about reaching the *open* position.
CONTACT = "binary_sensor.vacuum_garage_door_contact_state"


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
        """Makes a repeated edge harmless: no second pause behind a shut door.

        The signal changed on 2026-10-14. It used to be `current_position > 5`,
        which the echo satisfies -- the device reports the commanded position the
        instant it is commanded, so a door that is still shut can read 100. The
        contact cannot be echoed, so it is the one that can actually answer this.
        """
        branch = _branch(_config(), 0)
        assert any(
            condition.get("condition") == "state"
            and condition.get("entity_id") == CONTACT
            and condition.get("state") == "on"
            for condition in branch["conditions"]
        ), (
            "phase B must confirm the door has physically left the closed "
            "position before pausing the robot a second time"
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


def _resume_block() -> dict:
    """Return the guarded resume step in phase B.

    Phase B pauses, closes the door, and then has to start the task again. The
    resume is the one step in the automation that calls a service the
    integration owns, so it is also the one step that can fail for a reason the
    automation cannot see coming: the integration not being loaded.
    """
    for step in _branch(_config(), 0)["sequence"]:
        if "roborock_plus.resume_task" in json.dumps(step, ensure_ascii=False):
            return step
    raise AssertionError(
        "phase B no longer has a guarded roborock_plus.resume_task step"
    )


class TestTheResumeStepProvesItTook:
    """A resume that did not happen must not be announced as success.

    On 2026-10-13 21:47 -> 2026-10-14 01:18 the integration was down for about
    three and a half hours: HA could not install `python-roborock==5.5.1`, so
    `async_setup` never ran, none of the `roborock_plus.*` actions existed, and
    the vacuum entities read `unavailable`. Spook said so in as many words ("the
    automation references an action Home Assistant does not recognise"), and it
    was describing a real outage rather than a mistake in the automation.

    A bare call would have been worse than useless in that window. `resume_task`
    raises `resume_not_available` when the device state cannot say which command
    to resume with -- a documented consequence of a restart while paused -- and
    a missing action raises `ServiceNotFound`. The first aborts the run, the
    second is *deliberately* not swallowed by `continue_on_error`
    (`helpers/script.py`: `ServiceNotFound` is re-raised). Either way the run
    stops before the success notice, and the owner is never told that the robot
    is sitting paused behind a closed door.
    """

    SERVICE = "roborock_plus.resume_task"

    def test_the_call_is_tolerated_so_a_refusal_does_not_kill_the_run(self) -> None:
        """`resume_not_available` must reach the guard, not end the run."""
        step = next(
            s for s in _resume_block()["then"] if s.get("action") == self.SERVICE
        )
        assert step.get("continue_on_error") is True, (
            "without continue_on_error a resume_not_available refusal (restart "
            "while paused) aborts the automation before any alert is reached"
        )

    def test_the_resume_is_verified_by_observation(self) -> None:
        """Not erroring is not the same as working; wait for the state to move."""
        then = _resume_block()["then"]
        call_index = next(
            i for i, s in enumerate(then) if s.get("action") == self.SERVICE
        )
        wait = next((s for s in then[call_index + 1 :] if "wait_template" in s), None)
        assert wait is not None, (
            "the resume must be followed by a wait on the vacuum's state; "
            "'the action did not raise' cannot tell a resumed robot from a "
            "paused one"
        )
        assert wait.get("continue_on_timeout") is True
        template = wait["wait_template"]
        assert "is_state" in template and "paused" in template, (
            "the wait must require the vacuum to have left `paused`, which is "
            f"the state phase B confirmed before closing the door; got {template!r}"
        )
        assert "not " in template, (
            f"the wait must be satisfied by leaving `paused`, not by being in "
            f"it; got {template!r}"
        )

    def test_a_failed_resume_alerts_and_stops(self) -> None:
        then = _resume_block()["then"]
        guards = [s for s in then if "if" in s]
        assert guards, "the resume has no timeout guard"
        failure = json.dumps(guards[-1]["then"], ensure_ascii=False)
        assert "alert_notify" in failure, (
            "a resume that did not take must reach the owner; the door is shut "
            "and the robot is paused, so silence looks like success"
        )
        assert "stop" in failure, "the failure path must stop, not fall through"

    def test_an_unavailable_integration_is_refused_before_the_call(self) -> None:
        """`continue_on_error` cannot save a run from a missing action.

        HA re-raises `ServiceNotFound` even when `continue_on_error` is set, so
        the only way to warn about an unloaded integration is to not call the
        action at all. When the component is not set up, its entities are
        `unavailable` -- the same condition, visible from the automation.
        """
        block = _resume_block()
        conditions = json.dumps(block["if"], ensure_ascii=False)
        assert "unavailable" in conditions, (
            "the resume must first check the integration is actually present; "
            "calling a missing action aborts the run before the alert"
        )
        else_branch = json.dumps(block.get("else"), ensure_ascii=False)
        assert "alert_notify" in else_branch, (
            "an unavailable integration must be reported, not skipped silently"
        )
        assert "stop" in else_branch, (
            "the refusal path must stop before the success notice, or the "
            "automation claims a resume it never attempted"
        )

    def test_success_is_announced_only_after_the_verification(self) -> None:
        """The notice must not be reachable from the failure paths."""
        sequence = _branch(_config(), 0)["sequence"]
        assert not any(
            "已恢复" in json.dumps(step, ensure_ascii=False)
            for step in _resume_block()["then"]
        ), "the success notice sits inside the resume block, where a failed resume can reach it"

        resume_index = next(
            i
            for i, step in enumerate(sequence)
            if "roborock_plus.resume_task" in json.dumps(step, ensure_ascii=False)
        )
        notice_index = next(
            (
                i
                for i, step in enumerate(sequence)
                if "已恢复" in json.dumps(step, ensure_ascii=False)
            ),
            None,
        )
        assert notice_index is not None, "the success notice is missing entirely"
        assert notice_index > resume_index


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
    """Return (path, wait_step, preceding_step) for the echo-prone door waits.

    These are the waits that still read `current_position`, which the device
    echoes: the moment any motor command is issued it writes the *target* into
    the attribute, so a position-only wait passes instantly. As of 2026-10-14
    only the door-*opening* wait works this way -- there is no contact at the open
    end of the travel -- so this finds one. `_contact_waits()` covers the other
    side.

    Recursive, because a wait may sit inside `if/then`. `preceding_step` is the
    sibling immediately before the wait in the same list, which is where the
    settle delay must be inserted.
    """
    return _find_waits(lambda step: "current_position" in step["wait_template"])


def _contact_waits() -> list[tuple[str, dict, object]]:
    """Return (path, wait_step, preceding_step) for the contact-based door waits.

    The mirror of `_door_waits()`: these must NOT carry a settle delay, because
    the whole reason for moving the judgement to a physical switch is that it
    cannot be echoed. A 45-second delay in front of one would mean the automation
    waits for no reason on every close.
    """
    return _find_waits(
        lambda step: CONTACT in step["wait_template"] and "current_position" not in step["wait_template"]
    )


def _find_waits(predicate) -> list[tuple[str, dict, object]]:
    """Scan every wait_template in the action tree matching `predicate`."""
    config = _config()
    found: list[tuple[str, dict, object]] = []

    def visit_list(items: list, path: str) -> None:
        for index, item in enumerate(items):
            child = f"{path}[{index}]"
            if (
                isinstance(item, dict)
                and "wait_template" in item
                and predicate(item)
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

    As of 2026-10-14 the closing waits no longer read position at all -- they read
    the contact at the closed position, which the device cannot echo. Only the
    door-*opening* wait still needs the settle, because there is no contact at
    the open end of the travel. `TestContactWaitsNeedNoSettle` covers the rest.
    """

    def test_the_opening_wait_is_found(self) -> None:
        """Guard the scan itself: a structural change must not silently skip one."""
        paths = [path for path, _, _ in _door_waits()]
        assert len(paths) == 1, (
            f"expected 1 echo-prone (position) door wait, found {len(paths)}: "
            f"{paths}. If the automation legitimately changed, update this count "
            "-- but check you are not just failing to see a nested wait."
        )
        assert "choose[1]" in paths[0], (
            f"the remaining position wait should be the opening one in phase C; "
            f"found {paths}"
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


class TestContactWaitsNeedNoSettle:
    """The closing waits must trust the contact, not re-add the old wait.

    Moving the judgement to a physical switch is only an improvement if the
    45-second echo settle goes away with it. Leaving it in place would mean every
    close stalls for three quarters of a minute waiting on a signal that cannot
    be wrong -- and worse, it would hide the fact that the contact is what is
    being trusted.
    """

    def test_both_closing_waits_use_the_contact(self) -> None:
        paths = [path for path, _, _ in _contact_waits()]
        assert len(paths) == 2, (
            f"expected the two closing waits (phase B and phase D) to read the "
            f"contact, found {len(paths)}: {paths}"
        )
        assert any("choose[0]" in path for path in paths), "phase B is missing"
        assert any("choose[2]" in path for path in paths), (
            "phase D's wait is nested inside if/then; the scan must still find it"
        )

    def test_no_contact_wait_is_preceded_by_an_echo_settle(self) -> None:
        for path, _, previous in _contact_waits():
            if previous is None:
                continue
            assert "delay" not in previous or _delay_seconds(previous["delay"]) < _echo_settle_seconds(), (
                f"{path} reads the contact but still waits out the echo window "
                "first; the contact cannot be echoed, so that delay is dead time"
            )

    def test_no_closing_wait_reads_the_echoed_position(self) -> None:
        """The regression: a closing wait that silently reverts to position."""
        blob = json.dumps(_config(), ensure_ascii=False)
        assert CONTACT in blob, "the closing judgement left the contact sensor"

    def test_the_paused_wait_is_not_given_a_door_delay(self) -> None:
        """The `paused` wait is about the vacuum, not the door.

        Checked against the raw config rather than through `_door_waits()`: that
        helper only returns steps whose template already contains
        `current_position`, so asserting on its output would be a tautology and
        would not notice the `paused` wait gaining a delay.
        """
        sequence = _branch(_config(), 0)["sequence"]
        paused = [
            (index, step)
            for index, step in enumerate(sequence)
            if "wait_template" in step and "is_state" in step["wait_template"]
        ]
        assert paused, "the pause-confirmation wait is missing from branch 0"

        for index, _step in paused:
            neighbours = sequence[max(0, index - 1) : index + 1]
            assert not any("delay" in step for step in neighbours), (
                "the paused wait is about the vacuum, not the door; giving it the "
                "door settle delay would stall every departure for no reason"
            )

    def test_every_door_wait_keeps_a_budget_for_a_slow_door(self) -> None:
        """The settle must be followed by time to observe arrival.

        The settle alone is not enough: the door still has to be seen reaching
        the position. Reverting the budget to the old 60s would leave only 15s
        after the 45s settle, so the timeout is pinned here -- an in-memory
        mutation showed it was otherwise unguarded.
        """
        settle = _echo_settle_seconds()
        for path, step, _ in _door_waits():
            budget = _delay_seconds(step["timeout"])
            assert budget >= settle + 30, (
                f"{path} allows only {budget}s total, which leaves "
                f"{budget - settle}s to observe the door after the {settle}s "
                "settle; a genuinely slow door would be reported as a failure"
            )
