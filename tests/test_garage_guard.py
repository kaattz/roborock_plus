from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "garage_guard.py"
)
SPEC = spec_from_file_location("roborock_plus_garage_guard", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_guard_handles_start_segment_and_zoned_clean_commands() -> None:
    assert MODULE.should_guard_clean_command("app_start")
    assert MODULE.should_guard_clean_command("app_segment_clean")
    assert MODULE.should_guard_clean_command("app_zoned_clean")
    assert MODULE.should_guard_clean_command("APP_START")
    assert MODULE.should_guard_clean_command("APP_SEGMENT_CLEAN")
    assert MODULE.should_guard_clean_command("APP_ZONED_CLEAN")


def test_guard_ignores_non_start_commands() -> None:
    assert not MODULE.should_guard_clean_command("app_pause")
    assert not MODULE.should_guard_clean_command("find_me")


def test_cover_position_at_least_95_is_open_enough() -> None:
    assert not MODULE.is_garage_door_open_enough(None)
    assert not MODULE.is_garage_door_open_enough(94.9)
    assert MODULE.is_garage_door_open_enough(95)
    assert MODULE.is_garage_door_open_enough(100)


def test_garage_guard_requires_only_enabled_and_cover() -> None:
    assert not MODULE.is_garage_guard_ready({})
    assert MODULE.is_garage_guard_ready(
        {
            MODULE.CONF_GARAGE_GUARD_ENABLED: True,
            MODULE.CONF_GARAGE_DOOR_ENTITY_ID: "cover.vacuum_garage_door",
        }
    )
