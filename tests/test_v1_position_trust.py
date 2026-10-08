"""Tests for the vacuum position trust resolution.

These cover the failure measured on 2026-10-08: the robot sat on its dock for
thirteen hours while the map payload kept returning the living room, and
`outside_danger_zone` reported that closing the door was safe while the robot was
standing in the danger zone.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "roborock_plus"
    / "v1_position_trust.py"
)

SPEC = importlib.util.spec_from_file_location("v1_position_trust_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

DEFAULT_DOCK_TOLERANCE = MODULE.DEFAULT_DOCK_TOLERANCE
is_docked_state = MODULE.is_docked_state
resolve_position_trust = MODULE.resolve_position_trust

# The values measured on the live device.
DOCK = SimpleNamespace(x=25688, y=28538)
STALE_LIVING_ROOM = SimpleNamespace(x=25727, y=24232)


class TestIsDockedState:
    def test_charging_is_docked(self) -> None:
        assert is_docked_state("charging") is True

    def test_state_enum_like_object_is_read_by_name(self) -> None:
        assert is_docked_state(SimpleNamespace(name="charging_complete")) is True

    def test_idle_is_not_treated_as_docked(self) -> None:
        """`idle` can mean parked in a room, so the dock must not be substituted."""
        assert is_docked_state("idle") is False

    def test_cleaning_is_not_docked(self) -> None:
        assert is_docked_state("cleaning") is False

    def test_returning_is_not_docked(self) -> None:
        assert is_docked_state("returning_home") is False


class TestNotDocked:
    def test_reported_position_is_used(self) -> None:
        result = resolve_position_trust(
            state="cleaning", position=STALE_LIVING_ROOM, charger=DOCK
        )
        assert result.trusted is True
        assert (result.x, result.y) == (25727, 24232)
        assert result.reason == "not_docked"
        assert result.from_dock is False

    def test_missing_position_is_untrusted(self) -> None:
        result = resolve_position_trust(state="cleaning", position=None, charger=DOCK)
        assert result.trusted is False
        assert result.x is None and result.y is None


class TestDocked:
    def test_stale_living_room_position_is_replaced_by_the_dock(self) -> None:
        """The exact live failure: docked, but the map still says living room."""
        result = resolve_position_trust(
            state="charging", position=STALE_LIVING_ROOM, charger=DOCK
        )
        assert result.trusted is True
        assert (result.x, result.y) == (25688, 28538)
        assert result.reason == "docked_stale_position_replaced"
        assert result.from_dock is True

    def test_position_agreeing_with_the_dock_is_kept(self) -> None:
        result = resolve_position_trust(
            state="charging",
            position=SimpleNamespace(x=25689, y=28537),
            charger=DOCK,
        )
        assert result.trusted is True
        assert (result.x, result.y) == (25689, 28537)
        assert result.reason == "docked_at_charger"
        assert result.from_dock is False

    def test_jitter_well_inside_tolerance_is_kept(self) -> None:
        result = resolve_position_trust(
            state="charging",
            position=SimpleNamespace(x=25688 + 900, y=28538),
            charger=DOCK,
        )
        assert result.reason == "docked_at_charger"

    def test_just_outside_tolerance_is_replaced(self) -> None:
        result = resolve_position_trust(
            state="charging",
            position=SimpleNamespace(x=25688 + DEFAULT_DOCK_TOLERANCE + 1, y=28538),
            charger=DOCK,
        )
        assert result.reason == "docked_stale_position_replaced"

    def test_docked_without_a_charger_marker_is_refused(self) -> None:
        """No corroboration: refuse, because 'unknown' keeps the door open."""
        result = resolve_position_trust(
            state="charging", position=STALE_LIVING_ROOM, charger=None
        )
        assert result.trusted is False
        assert result.x is None and result.y is None
        assert result.reason == "docked_without_dock_reference"

    def test_docked_without_a_charger_marker_refuses_even_a_plausible_position(
        self,
    ) -> None:
        result = resolve_position_trust(
            state="charging",
            position=SimpleNamespace(x=25688, y=28538),
            charger=None,
        )
        assert result.trusted is False

    def test_docked_with_missing_position_falls_back_to_the_dock(self) -> None:
        result = resolve_position_trust(state="charging", position=None, charger=DOCK)
        assert result.trusted is True
        assert (result.x, result.y) == (25688, 28538)
        assert result.from_dock is True


class TestMeasuredMargin:
    def test_live_stale_offset_exceeds_tolerance_by_a_wide_margin(self) -> None:
        """Documents the gap the tolerance sits in: jitter is ~1px, error ~4300."""
        offset = ((25727 - 25688) ** 2 + (24232 - 28538) ** 2) ** 0.5
        assert offset > 4000
        assert DEFAULT_DOCK_TOLERANCE < offset / 4

    def test_live_stale_and_dock_land_on_opposite_sides_of_the_zone(self) -> None:
        """The danger: the stale reading says 'clear' while the dock is inside."""
        zone_min_y, zone_max_y = 27123, 28764
        assert 24232 < zone_min_y  # stale reading: outside (looks clear)
        assert zone_min_y <= 28538 <= zone_max_y  # the dock: inside (not clear)


class TestMalformedInput:
    def test_non_numeric_coordinates_are_refused(self) -> None:
        result = resolve_position_trust(
            state="charging",
            position=SimpleNamespace(x="abc", y="def"),
            charger=DOCK,
        )
        assert result.trusted is True  # falls back to the dock
        assert (result.x, result.y) == (25688, 28538)

    def test_charger_with_non_numeric_coordinates_is_refused(self) -> None:
        result = resolve_position_trust(
            state="charging",
            position=STALE_LIVING_ROOM,
            charger=SimpleNamespace(x="abc", y="def"),
        )
        assert result.trusted is False
