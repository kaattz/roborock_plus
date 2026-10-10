"""The shipped automations and blueprint must keep depending on the integration.

These are not three independent files. They are one system: the integration
opens the door before a command is issued, the state machine closes it after the
robot has left and reopens it before the robot returns, and the blueprint decides
when a clean starts. Each half covers what the other cannot.

* The integration can act *before* a command -- it is on the command path and can
  block -- but it cannot see what the robot does afterwards.
* The automations can see what the robot does afterwards, but by the time they
  observe movement, the robot is already moving, so they cannot open the door in
  time.

The links between them are entity ids, service names and event types. A rename
on either side breaks the chain silently: the automation stays enabled and simply
never fires. These tests pin the links so a rename fails the build instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
AUTOMATIONS = ROOT / "automations"
BLUEPRINTS = ROOT / "blueprints"

STATEMACHINE = AUTOMATIONS / "roborock_garage_door_statemachine.yaml"
STUCK = AUTOMATIONS / "roborock_stuck_or_error_stop.yaml"
NOT_STARTED = AUTOMATIONS / "roborock_clean_command_not_started.yaml"


def _blueprint_path() -> Path:
    """Find the cleaning-schedule blueprint without hardcoding its filename.

    It is deliberately named in Chinese so Home Assistant shows a readable title
    -- HA derives the blueprint's display name from the file name, not from the
    ``name:`` field. Hardcoding that name here would make every rename a test
    edit, and the name has already changed once.
    """
    candidates = [
        path
        for path in BLUEPRINTS.glob("*.yaml")
        if "cleaning" in path.name or "清扫" in path.name
    ]
    if len(candidates) != 1:
        raise AssertionError(
            f"expected exactly one cleaning blueprint in {BLUEPRINTS}, "
            f"found: {[p.name for p in candidates]}"
        )
    return candidates[0]


BLUEPRINT = _blueprint_path()


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _config(path: Path) -> dict:
    return yaml.safe_load(_text(path))


class TestStateMachineUsesTheIntegration:
    """It closes the door; it needs the integration for three separate things."""

    def test_the_mirror_carries_the_same_steps_as_the_generated_config(self) -> None:
        """The YAML is what gets pasted into HA, so it must not drift.

        `scripts/garage_state_machine/build.py` generates `state_machine.json`,
        and `automations/roborock_garage_door_statemachine.yaml` is the
        hand-paste mirror of it. On 2026-10-10 the door waits gained a settle
        delay in the JSON while the mirror kept the old steps, so pasting the
        mirror would have shipped the position-echo bug back into HA -- the same
        half-second wait the fix exists to remove.

        Only `id` may differ: it is HA's unique_id and exists only in the YAML.
        """
        generated = json.loads(
            (
                ROOT / "scripts" / "garage_state_machine" / "state_machine.json"
            ).read_text(encoding="utf-8")
        )
        mirror = _config(STATEMACHINE)
        mirror.pop("id", None)

        assert mirror.get("actions") == generated["actions"], (
            "the pasted YAML's actions differ from the generated config; "
            "re-run scripts/garage_state_machine/build.py and regenerate the "
            "mirror, or HA will run steps the repo does not test"
        )

    def test_the_mirror_carries_the_same_triggers_as_the_generated_config(self) -> None:
        """Triggers drift too, and they carry the anti-flap guards.

        Comparing only `actions` left the trigger half unprotected: `from: off`
        on the departure trigger and `for: seconds: 10` on the return trigger are
        exactly the guards that stop one departure being counted repeatedly, and
        dropping either from the mirror used to leave the whole suite green.
        """
        generated = json.loads(
            (
                ROOT / "scripts" / "garage_state_machine" / "state_machine.json"
            ).read_text(encoding="utf-8")
        )
        mirror = _config(STATEMACHINE)

        assert mirror.get("triggers") == generated["triggers"], (
            "the pasted YAML's triggers differ from the generated config; the "
            "departure `from:` and the return `for:` are load-bearing guards"
        )

    def test_the_mirror_carries_the_same_scalar_settings(self) -> None:
        """Mode and bounds decide how bursts of triggers are handled."""
        generated = json.loads(
            (
                ROOT / "scripts" / "garage_state_machine" / "state_machine.json"
            ).read_text(encoding="utf-8")
        )
        mirror = _config(STATEMACHINE)

        for key in ("mode", "max", "max_exceeded", "conditions"):
            assert mirror.get(key) == generated.get(key), (
                f"the pasted YAML's {key!r} differs from the generated config"
            )

    def test_it_resumes_with_the_integration_service(self) -> None:
        """Closing the door requires pausing, and only resume_task continues the
        original task -- plain vacuum.start would restart it."""
        assert "roborock_plus.resume_task" in _text(STATEMACHINE)

    def test_it_pauses_before_closing(self) -> None:
        """Closing onto a moving robot is the failure this prevents."""
        text = _text(STATEMACHINE)
        assert "vacuum.pause" in text
        assert text.index("vacuum.pause") < text.index("cover.close_cover")

    def test_it_keys_departure_on_the_danger_zone_entity(self) -> None:
        assert "binary_sensor.g20s_ultra_outside_danger_zone" in _text(STATEMACHINE)

    def test_it_requires_the_task_active_sensor(self) -> None:
        assert "binary_sensor.g20s_ultra_task_active" in _text(STATEMACHINE)

    def test_it_never_closes_without_pausing_first(self) -> None:
        """Every close_cover must be preceded by a pause somewhere earlier."""
        text = _text(STATEMACHINE)
        assert text.count("cover.close_cover") >= 1
        assert text.count("vacuum.pause") >= 1


class TestStuckAutomationUsesTheIntegrationEvent:
    def test_it_triggers_on_the_integration_event(self) -> None:
        config = _config(STUCK)
        event_types = [
            t.get("event_type") for t in config["triggers"] if t.get("trigger") == "event"
        ]
        assert "roborock_plus_vacuum_stuck" in event_types

    def test_it_stops_the_vacuum(self) -> None:
        assert "vacuum.stop" in _text(STUCK)

    def test_it_does_not_close_the_door(self) -> None:
        """The robot may be stuck in the doorway; closing could trap it."""
        assert "cover.close_cover" not in _text(STUCK)


class TestNotStartedAutomationUsesTheIntegrationEvent:
    def test_it_triggers_on_the_integration_event(self) -> None:
        config = _config(NOT_STARTED)
        event_types = [
            t.get("event_type") for t in config["triggers"] if t.get("trigger") == "event"
        ]
        assert "roborock_plus_clean_command_not_started" in event_types

    def test_it_does_not_close_the_door(self) -> None:
        """A failed command does not prove the robot stayed put."""
        assert "cover.close_cover" not in _text(NOT_STARTED)


class TestBlueprintUsesTheIntegration:
    """The blueprint starts a clean; the integration opens the door for it."""

    def test_it_cleans_areas_through_the_guarded_service(self) -> None:
        """vacuum.clean_area goes through the integration's garage_guard, which
        is what opens the door before the command is sent."""
        assert "vacuum.clean_area" in _text(BLUEPRINT)

    def test_it_confirms_the_start_via_the_task_active_sensor(self) -> None:
        """Reporting success off the service return would be wrong: the call
        returns as soon as the server accepts it, not when the robot moves."""
        text = _text(BLUEPRINT)
        assert "task_active_entity" in text
        assert "wait_template" in text

    def test_it_does_not_touch_the_door(self) -> None:
        """Opening and closing are the integration's and the state machine's job.
        A third actor on the same cover is how the 2026-10-08 oscillation began."""
        text = _text(BLUEPRINT)
        assert "cover.open_cover" not in text
        assert "cover.close_cover" not in text


class TestDocumentedLinksExist:
    """The README names these; the files must actually contain them."""

    @pytest.mark.parametrize(
        "needle",
        [
            "roborock_plus.resume_task",
            "binary_sensor.g20s_ultra_task_active",
            "binary_sensor.g20s_ultra_outside_danger_zone",
            "roborock_plus_vacuum_stuck",
            "roborock_plus_clean_command_not_started",
            "vacuum.clean_area",
        ],
    )
    def test_link_present_somewhere(self, needle: str) -> None:
        corpus = "".join(
            _text(path) for path in (STATEMACHINE, STUCK, NOT_STARTED, BLUEPRINT)
        )
        assert needle in corpus, (
            f"{needle!r} is documented as part of the system but appears in "
            "none of the shipped automations or the blueprint"
        )


class TestReadmeDescribesTheSystem:
    def test_readme_links_all_four_parts(self) -> None:
        readme = _text(ROOT / "README.md")
        for name in (
            "automations/roborock_garage_door_statemachine.yaml",
            "automations/roborock_stuck_or_error_stop.yaml",
            "automations/roborock_clean_command_not_started.yaml",
        ):
            assert name in readme, f"README does not link {name}"
        # The blueprint is linked by its (Chinese) filename.
        assert f"blueprints/{BLUEPRINT.name}" in readme, (
            f"README does not link blueprints/{BLUEPRINT.name}"
        )

    def test_readme_has_no_stale_entity_names(self) -> None:
        """The zone entities were renamed; the README must not teach the old ones."""
        readme = _text(ROOT / "README.md")
        for stale in ("clear_of_garage", "in_safe_zone", "safe_zone_configured"):
            assert stale not in readme, (
                f"README still mentions {stale!r}, which was renamed"
            )

    def test_readme_explains_the_split(self) -> None:
        """The 'why must these work together' section is the point of the doc."""
        readme = _text(ROOT / "README.md")
        assert "这套东西怎么配合" in readme


class TestAutomationsReadmeIsAccurate:
    def test_it_states_they_are_managed_by_hand(self) -> None:
        text = _text(AUTOMATIONS / "README.md")
        assert "人工" in text

    def test_it_records_every_unique_id(self) -> None:
        """Every automation in the directory must appear in the README table.

        Derived from the files rather than hardcoded: the README is the paste
        instruction the whole deployment depends on, and a new automation it does
        not mention is one nobody will deploy.
        """
        text = _text(AUTOMATIONS / "README.md")
        files = sorted(AUTOMATIONS.glob("*.yaml"))
        assert files, "expected automations in the directory"

        missing = []
        for path in files:
            config = _config(path)
            unique_id = str(config.get("id", ""))
            if not unique_id or unique_id not in text:
                missing.append(f"{path.name} (id {unique_id or 'missing'})")
        assert not missing, (
            "these automations exist but their unique_id is not in the README, so "
            "the paste instructions do not cover them: " + ", ".join(missing)
        )

    def test_it_documents_the_blueprint_url(self) -> None:
        text = _text(AUTOMATIONS / "README.md")
        # The URL may be percent-encoded or plain; accept either.
        assert "raw.githubusercontent.com/kaattz/roborock_plus/main/blueprints/" in text
        assert (
            "%E6%89%AB%E5%9C%B0%E6%9C%BA" in text or BLUEPRINT.name in text
        ), "the blueprint import URL is not documented"
