"""Wiring tests for the position-trust fix.

The pure-logic tests in `test_v1_position_trust.py` prove the decision table.
These prove the decision is actually reached from the entities and services,
which pure tests cannot show: a fix that is correct but never called looks
identical to no fix at all.
"""

from __future__ import annotations

from pathlib import Path


COMPONENT = (
    Path(__file__).resolve().parent.parent / "custom_components" / "roborock_plus"
)


def _read(name: str) -> str:
    return (COMPONENT / name).read_text(encoding="utf-8")


class TestBinarySensorWiring:
    def test_position_helper_delegates_to_the_coordinator(self) -> None:
        source = _read("binary_sensor.py")
        assert "resolved = coordinator.resolve_vacuum_position()" in source

    def test_fresh_helper_refuses_untrusted_samples(self) -> None:
        source = _read("binary_sensor.py")
        assert "if not resolved.trusted or resolved.x is None or resolved.y is None:" in source
        assert "return None" in source

    def test_entities_no_longer_read_the_raw_map_position(self) -> None:
        """The raw field is a stale snapshot; neither entity may consult it."""
        source = _read("binary_sensor.py")
        assert "map_data.vacuum_position" not in source

    def test_danger_sensor_returns_none_when_untrusted(self) -> None:
        """`clear_of_garage` must go unknown, not report a stale 'clear'."""
        source = _read("binary_sensor.py")
        assert "return point_clear_of_garage(position[0], position[1], stored.zone)" in source

    def test_both_entities_route_through_the_helper(self) -> None:
        source = _read("binary_sensor.py")
        assert source.count("_fresh_vacuum_position(self.coordinator)") == 2


class TestCoordinatorWiring:
    def test_coordinator_exposes_the_resolver(self) -> None:
        source = _read("coordinator.py")
        assert "def resolve_vacuum_position(self) -> ResolvedPosition:" in source

    def test_resolver_checks_freshness_before_trusting(self) -> None:
        source = _read("coordinator.py")
        assert 'return ResolvedPosition(None, None, False, "sample_too_old")' in source

    def test_resolver_passes_state_position_and_charger(self) -> None:
        source = _read("coordinator.py")
        assert "state=self.properties_api.status.state," in source
        assert "position=None if map_data is None else map_data.vacuum_position," in source
        assert "charger=None if map_data is None else map_data.charger," in source

    def test_module_imports_the_trust_helper(self) -> None:
        source = _read("coordinator.py")
        assert "from .v1_position_trust import ResolvedPosition, resolve_position_trust" in source


class TestServiceWiring:
    def test_position_service_uses_the_resolved_value(self) -> None:
        source = _read("vacuum.py")
        assert source.count("resolved = self.coordinator.resolve_vacuum_position()") == 2

    def test_position_service_records_its_own_fresh_read(self) -> None:
        """A fresh on-demand read must not be rejected as an old background sample."""
        source = _read("vacuum.py")
        assert source.count("self.coordinator.note_map_position_sample()") == 2
        assert "        if refreshed:\n            self.coordinator.note_map_position_sample()\n" in source
        assert (
            "        if await self._async_try_refresh_map_content(map_content_trait):\n"
            "            self.coordinator.note_map_position_sample()\n"
        ) in source

    def test_position_service_stale_is_not_merely_read_success(self) -> None:
        source = _read("vacuum.py")
        assert '"stale": (not refreshed) or resolved.from_dock,' in source
        assert '"stale": not refreshed,' not in source

    def test_position_service_stale_still_reports_a_failed_read(self) -> None:
        """A failed read must not be masked by the dock substitution."""
        source = _read("vacuum.py")
        assert '"stale": resolved.from_dock,' not in source
        assert "not refreshed" in source

    def test_position_service_refuses_an_untrusted_answer(self) -> None:
        source = _read("vacuum.py")
        assert "if not resolved.trusted or resolved.x is None or resolved.y is None:" in source

    def test_editor_context_resolves_before_the_map_guard(self) -> None:
        """`resolved` is used by the result dict, so it must bind unconditionally."""
        source = _read("vacuum.py")
        marker = "        resolved = self.coordinator.resolve_vacuum_position()\n"
        assert marker in source
        assert source.index(marker) < source.index(
            "        if map_content_trait.map_data is not None:\n"
        )

    def test_editor_context_reports_provenance(self) -> None:
        source = _read("vacuum.py")
        assert '"position_trusted": resolved.trusted,' in source
        assert '"position_source": resolved.reason,' in source
