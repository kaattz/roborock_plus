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
        """`outside_danger_zone` must go unknown, not report a stale 'outside'."""
        source = _read("binary_sensor.py")
        assert (
            "return point_outside_danger_zone(position[0], position[1], stored.zone)"
            in source
        )

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


class TestImageWiring:
    """The map PNG carries its own copy of the position, so it needs its own fix.

    Correcting the services and entities is not enough: the library bakes the
    robot marker into the image at parse time, so the picture kept showing a
    docked robot in the room the previous task ended in.
    """

    def test_image_entity_rerenders_the_marker(self) -> None:
        source = _read("image.py")
        assert "from .v1_map_render import rerender_map_image" in source
        assert "rerender_map_image," in source

    def test_image_entity_passes_the_resolved_position(self) -> None:
        source = _read("image.py")
        assert "resolved = self.coordinator.resolve_vacuum_position()" in source
        assert "resolved_x=resolved.x,\n" in source
        assert "resolved_y=resolved.y,\n" in source
        assert "trusted=resolved.trusted," in source
        assert "from_dock=resolved.from_dock," in source

    def test_image_entity_does_not_pass_the_drawn_position_as_resolved(self) -> None:
        """Passing the drawn value as `resolved` would always cancel the redraw."""
        source = _read("image.py")
        assert "resolved_x=map_data.vacuum_position.x,\n" not in source
        assert "resolved_y=map_data.vacuum_position.y,\n" not in source

    def test_image_entity_passes_the_drawn_position(self) -> None:
        """The decision needs both where it is and where it was drawn."""
        source = _read("image.py")
        assert "map_data.vacuum_position.x, map_data.vacuum_position.y" in source

    def test_image_entity_falls_back_to_the_library_image(self) -> None:
        source = _read("image.py")
        assert "if corrected is None:\n            return image\n" in source

    def test_image_entity_reports_a_missing_map_rather_than_a_blank(self) -> None:
        source = _read("image.py")
        assert '"Map flag not found in coordinator maps"' in source

    def test_image_rendering_runs_off_the_event_loop(self) -> None:
        """Re-parsing a map is synchronous CPU work on the loop otherwise."""
        source = _read("image.py")
        assert "await self.hass.async_add_executor_job(" in source
        assert "partial(\n                rerender_map_image," in source

    def test_image_entity_caches_the_corrected_render(self) -> None:
        """A dashboard polling the image must not re-render an identical map."""
        source = _read("image.py")
        assert "self._corrected_cache" in source
        assert "self._corrected_cache[0] == cache_key" in source
        assert "self._corrected_cache = (cache_key, corrected)" in source

    def test_image_cache_key_covers_every_rendering_input(self) -> None:
        source = _read("image.py")
        for field in (
            "id(map_content.raw_api_response),",
            "drawn,",
            "resolved.x,",
            "resolved.y,",
            "resolved.trusted,",
            "resolved.from_dock,",
        ):
            assert field in source, f"{field} missing from the cache key"
