"""The docked-state and movement-state tables must not overlap.

Two tables in the integration describe the same hardware, and a state in both
is a real defect rather than a style question: `v1_position_trust` substitutes
the charger marker for a docked robot's position, and `v1_stuck_detection`
reads an unchanging position as "not moving". A state in both would therefore
declare a docked robot stuck and stop a clean that was working.

The tables answer different questions, so they are allowed to be wrong in
opposite directions on an ambiguous state. What they may not do is overlap.

This was a real bug: `back_to_dock_washing_duster` was in both, and only the
accident that the state has never been reported on this device kept it from
stopping a clean.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


COMPONENT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "roborock_plus"
)

AMBIGUOUS_STATE = "back_to_dock_washing_duster"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, COMPONENT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


trust = _load("trust_for_overlap_test", "v1_position_trust.py")
stuck = _load("stuck_for_overlap_test", "v1_stuck_detection.py")


class TestTablesAreDisjoint:
    def test_no_state_is_both_docked_and_assessed(self) -> None:
        overlap = trust.DOCKED_STATE_NAMES & stuck.MOVEMENT_STATES
        assert overlap == frozenset(), (
            f"{sorted(overlap)} appear in both tables: a docked robot's position "
            "would be read as evidence of standing still"
        )

    def test_both_tables_are_non_empty(self) -> None:
        """Guards against a typo that empties a table and passes trivially."""
        assert len(trust.DOCKED_STATE_NAMES) >= 5
        assert len(stuck.MOVEMENT_STATES) >= 10


class TestAmbiguousStateFailsSafely:
    def test_ambiguous_state_counts_as_docked(self) -> None:
        """Missing a docked robot lets the door close on it."""
        assert trust.is_docked_state(AMBIGUOUS_STATE) is True

    def test_ambiguous_state_is_not_assessed_for_movement(self) -> None:
        """Assessing a docked robot can stop a working clean."""
        assert stuck.should_assess_movement(state=AMBIGUOUS_STATE) is False

    def test_ambiguous_state_is_absent_from_the_movement_table(self) -> None:
        assert AMBIGUOUS_STATE not in stuck.MOVEMENT_STATES
        assert AMBIGUOUS_STATE in trust.DOCKED_STATE_NAMES


class TestKnownStatesAreClassifiedCorrectly:
    def test_charging_is_docked_and_not_assessed(self) -> None:
        assert trust.is_docked_state("charging") is True
        assert stuck.should_assess_movement(state="charging") is False

    def test_cleaning_is_assessed_and_not_docked(self) -> None:
        assert stuck.should_assess_movement(state="cleaning") is True
        assert trust.is_docked_state("cleaning") is False

    def test_docking_is_travel_so_it_is_assessed(self) -> None:
        """The robot is moving toward the dock, not sitting on it."""
        assert stuck.should_assess_movement(state="docking") is True
        assert trust.is_docked_state("docking") is False

    def test_returning_home_is_travel_so_it_is_assessed(self) -> None:
        assert stuck.should_assess_movement(state="returning_home") is True

    def test_mop_states_that_mean_travel_are_assessed(self) -> None:
        assert stuck.should_assess_movement(state="going_to_wash_the_mop") is True

    def test_mop_states_that_mean_at_the_dock_are_docked(self) -> None:
        assert trust.is_docked_state("washing_the_mop") is True
        assert stuck.should_assess_movement(state="washing_the_mop") is False

    def test_idle_is_neither_docked_nor_assessed(self) -> None:
        """`idle` is ambiguous in the other direction: it may be mid-room."""
        assert trust.is_docked_state("idle") is False
        assert stuck.should_assess_movement(state="idle") is False
