"""Tests for the position service's behaviour when a sample has aged out.

During a clean the service returned HTTP 500. Measured cause: a sample is
trusted for `2 x 5s = 10s`, while a map read takes a median of 6s and often 11s,
so between a sample expiring and its replacement arriving the resolver reports
`sample_too_old`. The service raised on that, even though a stale position is a
perfectly good answer to an *informational* query.

The safety entities must keep refusing in the same situation: an unknown answer
leaves the door open, a wrong one closes it on the robot. These tests pin both
halves, because the tempting "fix" -- making the resolver trust stale samples --
would fix the 500 by breaking the door.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

COMPONENT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "roborock_plus"
)


def _load(name: str, filename: str):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(name, COMPONENT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


trust = _load("trust_for_service_test", "v1_position_trust.py")


class TestDockedRobotIsNeverReportedOutsideTheZone:
    """The one direction that must not fail open."""

    def test_stale_position_is_replaced_by_the_charger(self) -> None:
        resolved = trust.resolve_position_trust(
            state="charging",
            position=type("P", (), {"x": 25727.0, "y": 24232.0})(),
            charger=type("P", (), {"x": 25688.0, "y": 28538.0})(),
        )
        assert resolved.trusted is True
        assert (resolved.x, resolved.y) == (25688.0, 28538.0)
        assert resolved.from_dock is True
        assert resolved.reason == "docked_stale_position_replaced"

    def test_matching_position_keeps_its_jitter(self) -> None:
        resolved = trust.resolve_position_trust(
            state="charging",
            position=type("P", (), {"x": 25690.0, "y": 28540.0})(),
            charger=type("P", (), {"x": 25688.0, "y": 28538.0})(),
        )
        assert resolved.trusted is True
        assert (resolved.x, resolved.y) == (25690.0, 28540.0)
        assert resolved.reason == "docked_at_charger"

    def test_no_charger_reference_refuses_rather_than_guesses(self) -> None:
        resolved = trust.resolve_position_trust(
            state="charging", position=None, charger=None
        )
        assert resolved.trusted is False
        assert resolved.reason == "docked_without_dock_reference"


class TestCleaningRobotUsesItsOwnPosition:
    def test_cleaning_position_is_taken_as_is(self) -> None:
        resolved = trust.resolve_position_trust(
            state="cleaning",
            position=type("P", (), {"x": 26000.0, "y": 24000.0})(),
            charger=type("P", (), {"x": 25688.0, "y": 28538.0})(),
        )
        assert resolved.trusted is True
        assert (resolved.x, resolved.y) == (26000.0, 24000.0)
        assert resolved.from_dock is False
        assert resolved.reason == "not_docked"

    def test_cleaning_without_a_position_is_untrusted(self) -> None:
        resolved = trust.resolve_position_trust(
            state="cleaning", position=None, charger=None
        )
        assert resolved.trusted is False
        assert resolved.reason == "missing_position"


class TestServiceFallsBackInsteadOfRaising:
    """The 500 was an informational query failing on an ordinary event."""

    def _shipped_method(self, name: str):
        """Extract a method body from the shipped vacuum.py."""
        source = (COMPONENT / "vacuum.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                return ast.get_source_segment(source, node)
        raise AssertionError(f"{name} not found in vacuum.py")

    def test_service_has_a_stale_fallback(self) -> None:
        body = self._shipped_method("get_vacuum_current_position")
        assert body is not None
        assert '"stale": True' in body, (
            "the service must answer with a stale position rather than raise"
        )

    def test_fallback_is_limited_to_an_aged_out_sample(self) -> None:
        """Only `sample_too_old` falls back, so a real failure still surfaces."""
        body = self._shipped_method("get_vacuum_current_position")
        assert body is not None
        assert 'resolved.reason != "sample_too_old"' in body
        assert 'translation_key="position_not_found"' in body

    def test_fallback_cannot_claim_the_robot_is_at_the_dock(self) -> None:
        """A fallback position is a raw map value, never a dock substitution."""
        body = self._shipped_method("get_vacuum_current_position")
        assert body is not None
        start = body.index('"stale": True')
        assert '"from_dock": False' in body[start:]

    def test_safety_entities_still_refuse_an_untrusted_sample(self) -> None:
        """The 500 fix must not leak into the door decision."""
        source = (COMPONENT / "binary_sensor.py").read_text(encoding="utf-8")
        assert "if not resolved.trusted or resolved.x is None or resolved.y is None:" in source
