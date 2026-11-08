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
        if ("cleaning" in path.name or "清扫" in path.name)
        and yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader).get("blueprint", {}).get("domain") == "automation"
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
    """The README names these; something shipped must actually provide them.

    Until 2026-11-08 the corpus included the hand-pasted garage-door state
    machine, which is where `resume_task`, `task_active` and `outside_danger_zone`
    were used. That automation was retired (the built-in door control does the
    job), so those three are now checked against the integration itself rather
    than against a YAML nobody installs.
    """

    COMPONENT = ROOT / "custom_components" / "roborock_plus"

    @pytest.mark.parametrize(
        "needle",
        [
            "resume_task",
            "task_active",
            "outside_danger_zone",
        ],
    )
    def test_integration_provides_it(self, needle: str) -> None:
        corpus = "".join(
            path.read_text(encoding="utf-8") for path in self.COMPONENT.glob("*.py")
        )
        assert needle in corpus, (
            f"{needle!r} is documented as part of the system but the integration "
            "does not mention it"
        )

    @pytest.mark.parametrize(
        "needle",
        [
            "roborock_plus_vacuum_stuck",
            "roborock_plus_clean_command_not_started",
            "vacuum.clean_area",
        ],
    )
    def test_shipped_automation_or_blueprint_uses_it(self, needle: str) -> None:
        corpus = "".join(_text(path) for path in (STUCK, NOT_STARTED, BLUEPRINT))
        assert needle in corpus, (
            f"{needle!r} is documented as part of the system but appears in "
            "none of the shipped automations or the blueprint"
        )


class TestReadmeDescribesTheSystem:
    def test_readme_does_not_advertise_retired_automations(self) -> None:
        """Two things were taken out of the README on 2026-11-08.

        The garage-door state machine was never installed in Home Assistant, and
        the two auxiliary alert automations are household-specific: they exist in
        this deployment's `automations.yaml`, but the README is shipped to other
        people, who do not have those entity event handlers wired up. Listing them
        only invites someone to paste automations that reference events they have
        not configured.
        """
        readme = _text(ROOT / "README.md")
        for retired in (
            "roborock_garage_door_statemachine.yaml",
            "roborock_stuck_or_error_stop.yaml",
            "roborock_clean_command_not_started.yaml",
        ):
            assert retired not in readme, f"README still advertises {retired}"

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
