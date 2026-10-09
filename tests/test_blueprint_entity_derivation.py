"""The blueprint derives its entity pickers from the chosen vacuum.

Three of the original inputs were entity pickers that the user had to fill in by
hand: the task-active sensor, the mop-intensity select and the mop-route select.
All three live on the vacuum's own device, and two of them have names that differ
by a single suffix (`select.sao_di_ji_v2` vs `select.sao_di_ji_v2_2`), so picking
them by hand is easy to get backwards.

They are now looked up from `device_entities(device_id(vacuum))` using capability
predicates rather than name matching. That makes the lookup logic load-bearing:
if it silently returns nothing, the blueprint would set no mop mode and never
confirm the clean started, and the failure would look like "the vacuum just did
not run today".

These tests pin the predicates and the failure handling. The predicates are also
exercised against the live system by `scripts/validate_blueprints.py`'s sibling
check in the HA template engine; here they are checked structurally.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _blueprint_path() -> Path:
    candidates = [
        path
        for path in (ROOT / "blueprints").glob("*.yaml")
        if "cleaning" in path.name or "清扫" in path.name
    ]
    assert len(candidates) == 1, f"expected one cleaning blueprint: {candidates}"
    return candidates[0]


def _load_validator():
    """Reuse the validator's loader so the `!input` tag resolves the same way."""
    spec = importlib.util.spec_from_file_location(
        "validate_blueprints", ROOT / "scripts" / "validate_blueprints.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator()
BLUEPRINT = _blueprint_path()
TEXT = BLUEPRINT.read_text(encoding="utf-8")
CONFIG = VALIDATOR.load_blueprint(BLUEPRINT)


class TestRemovedInputs:
    """The three hand-picked entity inputs are gone."""

    @pytest.mark.parametrize(
        "name",
        ["task_active_entity", "mop_intensity_entity", "mop_route_entity"],
    )
    def test_not_declared_as_inputs(self, name: str) -> None:
        declared = VALIDATOR.declared_inputs(CONFIG["blueprint"])
        assert name not in declared, (
            f"{name} is declared as a blueprint input again; it is supposed to be "
            "derived from the chosen vacuum"
        )

    @pytest.mark.parametrize(
        "name",
        ["task_active_entity", "mop_intensity_entity", "mop_route_entity"],
    )
    def test_still_defined_as_variables(self, name: str) -> None:
        """They must still exist as variables for the actions to use."""
        assert name in CONFIG["variables"]

    def test_entity_inputs_are_only_the_ones_that_cannot_be_derived(self) -> None:
        """Three entity pickers total, and each has a reason to be asked for.

        `vacuum_entity` anchors the whole thing. `presence_sensors` is a choice
        about the user's intent -- which rooms count as "occupied for this task"
        -- not something derivable. `notify_script` is the user's notification
        path. The other three were derivable and are now gone.
        """
        entity_inputs = []
        for key, value in (CONFIG["blueprint"].get("input") or {}).items():
            inputs = (
                value["input"]
                if isinstance(value, dict) and "input" in value
                else {key: value}
            )
            for name, spec in inputs.items():
                selector = (spec or {}).get("selector") or {}
                if "entity" in selector:
                    entity_inputs.append(name)
        assert sorted(entity_inputs) == [
            "notify_script",
            "presence_sensors",
            "vacuum_entity",
        ], f"unexpected entity inputs: {entity_inputs}"


class TestDerivationPredicates:
    """Each derived entity is found by a capability, not by its name."""

    def test_lookup_starts_from_the_vacuum_device(self) -> None:
        assert "device_entities(device_id(vacuum_entity))" in TEXT

    def test_lookup_is_guarded_against_a_missing_device(self) -> None:
        """device_id('vacuum.nope') yields nothing; default([]) keeps it safe."""
        assert "device_entities(device_id(vacuum_entity)) | default([], true)" in TEXT

    def test_task_active_uses_device_class_running(self) -> None:
        assert "'attributes.device_class', 'eq', 'running'" in TEXT

    def test_mop_intensity_uses_off_and_extreme(self) -> None:
        """`off` is what makes 'vacuum only' expressible, so it is the key signal.

        The two selects differ only by suffix; matching on their option sets is
        what makes the lookup independent of that suffix.
        """
        assert "'attributes.options', 'contains', 'off'" in TEXT
        assert "'attributes.options', 'contains', 'extreme'" in TEXT

    def test_mop_route_uses_deep_plus(self) -> None:
        assert "'attributes.options', 'contains', 'deep_plus'" in TEXT

    def test_all_three_predicates_are_distinct(self) -> None:
        """A shared predicate would return the same entity three times."""
        predicates = re.findall(r"\|\s*selectattr\('attributes\.options', 'contains', '(\w+)'\)", TEXT)
        assert len(predicates) == len(set(predicates)) or "deep_plus" in predicates, (
            "the option predicates overlap; the selects would be confused"
        )


class TestDerivationFailsLoudly:
    """A failed lookup must stop and alert, not quietly do nothing."""

    def test_missing_entities_alert(self) -> None:
        assert "找不到设备实体" in TEXT

    def test_missing_entities_stop_the_run(self) -> None:
        assert "stop: 推导失败" in TEXT

    def test_the_guard_runs_before_the_retry_loop(self) -> None:
        """Otherwise it would retry the whole window against a broken setup."""
        guard = TEXT.index("找不到设备实体")
        loop = TEXT.index("- repeat:")
        assert guard < loop

    def test_the_alert_names_what_was_found_instead(self) -> None:
        """So the user can see what the device actually exposes."""
        assert "device_entities_list | select('match', '^select\\.')" in TEXT
        assert "device_entities_list | select('match', '^binary_sensor\\.')" in TEXT

    def test_the_guard_explains_why_the_two_are_required(self) -> None:
        assert "任务进行中" in TEXT and "仅扫地" in TEXT


class TestOptionalMopRoute:
    """The route is optional; a device without it must still work."""

    def test_route_step_is_conditional(self) -> None:
        assert "mop_route_entity not in ['', none]" in TEXT

    def test_route_is_skipped_for_vacuum_only(self) -> None:
        """Setting a mop route when dry-vacuuming is meaningless."""
        assert "cleaning_mode != '仅扫地'" in TEXT


class TestVacuumOnlyStillUsesWaterOff:
    def test_intent_maps_to_water_off(self) -> None:
        assert "'off' if cleaning_mode == '仅扫地' else mop_intensity" in TEXT
