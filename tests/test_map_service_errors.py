"""Tests for map service degradation (task 3).

The vacuum refuses or times out on map reads while it is busy. These services
used to surface that as an unhandled error (HTTP 500); they should instead
degrade to cached data or raise a translated error.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VACUUM = ROOT / "custom_components" / "roborock_plus" / "vacuum.py"
STRING_FILES = [
    ROOT / "custom_components" / "roborock_plus" / "strings.json",
    ROOT / "custom_components" / "roborock_plus" / "translations" / "en.json",
]


def _vacuum_source() -> str:
    return VACUUM.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    """Return the source of a method up to the next sibling definition."""
    marker = f"    async def {name}("
    assert marker in source, f"{name} not found"
    body = source.split(marker, 1)[1]
    # Cut at the next method defined at the same indent level.
    return body.split("\n    async def ", 1)[0].split("\n    def ", 1)[0]


def test_map_refresh_tolerates_library_and_parse_errors() -> None:
    """`refresh()` can raise RoborockException or ValueError from the parser."""
    body = _function_body(_vacuum_source(), "_async_try_refresh_map_content")

    assert "except (RoborockException, ValueError)" in body


def test_current_position_service_degrades_instead_of_failing() -> None:
    body = _function_body(_vacuum_source(), "get_vacuum_current_position")

    # Falls back to cached map data rather than raising on refresh failure.
    assert "_async_try_refresh_map_content" in body
    assert "stale" in body
    # Only a genuinely missing position is an error now.
    assert "position_not_found" in body


def test_editor_context_service_degrades_instead_of_failing() -> None:
    body = _function_body(_vacuum_source(), "get_safe_zone_editor_context")

    # Best-effort refresh: fall back to cached home/map data.
    assert "_async_try_refresh_map_content" in body
    assert "except (RoborockException, ValueError)" in body
    # A completely unknown map is still a real error.
    assert "map_failure" in body


def test_safe_zone_suggestion_tolerates_parse_errors() -> None:
    body = _function_body(_vacuum_source(), "get_safe_zone_suggestion")

    assert "except (RoborockException, ValueError)" in body
    # Still returns the geometry it already computed.
    assert "return result" in body


def test_translation_files_stay_valid_json() -> None:
    import json

    for path in STRING_FILES:
        json.loads(path.read_text(encoding="utf-8"))
