from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "resume_logic.py"
)
SPEC = spec_from_file_location("roborock_plus_resume_logic", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

APP_RESUME_COMMAND = MODULE.APP_RESUME_COMMAND
select_resume_command = MODULE.select_resume_command
select_resume_command_for_clean_command = MODULE.select_resume_command_for_clean_command
select_resume_command_from_status = MODULE.select_resume_command_from_status
select_start_or_resume_command = MODULE.select_start_or_resume_command


def test_select_start_or_resume_command_for_returning() -> None:
    assert (
        select_start_or_resume_command(in_returning=1, in_cleaning=None)
        == APP_RESUME_COMMAND
    )


def test_select_start_or_resume_command_for_global_clean_resume() -> None:
    assert (
        select_start_or_resume_command(in_returning=None, in_cleaning=1)
        == APP_RESUME_COMMAND
    )


def test_select_start_or_resume_command_for_zoned_clean_resume() -> None:
    assert (
        select_start_or_resume_command(in_returning=None, in_cleaning=2)
        == APP_RESUME_COMMAND
    )


def test_select_start_or_resume_command_for_segment_clean_resume() -> None:
    assert (
        select_start_or_resume_command(in_returning=None, in_cleaning=3)
        == APP_RESUME_COMMAND
    )


def test_select_start_or_resume_command_for_build_map_resume() -> None:
    assert (
        select_start_or_resume_command(in_returning=None, in_cleaning=4)
        == "app_resume_build_map"
    )


def test_select_start_or_resume_command_for_new_start() -> None:
    assert (
        select_start_or_resume_command(in_returning=None, in_cleaning=0)
        == "app_start"
    )


def test_select_start_or_resume_command_uses_saved_context_when_paused() -> None:
    assert (
        select_start_or_resume_command(
            in_returning=0,
            in_cleaning=0,
            state="paused",
            fallback_command=APP_RESUME_COMMAND,
        )
        == APP_RESUME_COMMAND
    )


def test_select_resume_command_uses_fallback_when_paused_fields_are_cleared() -> None:
    assert (
        select_resume_command(
            in_returning=0,
            in_cleaning=0,
            state="paused",
            fallback_command=APP_RESUME_COMMAND,
        )
        == APP_RESUME_COMMAND
    )


def test_select_resume_command_does_not_guess_when_paused_context_is_missing() -> None:
    assert (
        select_resume_command(
            in_returning=0,
            in_cleaning=0,
            state="paused",
            fallback_command=None,
        )
        is None
    )


def test_select_resume_command_from_status_does_not_guess_paused() -> None:
    assert (
        select_resume_command_from_status(
            state="paused",
            in_returning=0,
            in_cleaning=0,
        )
        is None
    )


def test_select_resume_command_from_status_uses_segment_state() -> None:
    assert (
        select_resume_command_from_status(
            state="segment_cleaning",
            in_returning=0,
            in_cleaning=0,
        )
        == APP_RESUME_COMMAND
    )


def test_select_resume_command_from_status_uses_resume_for_returning_states() -> None:
    for state in ("returning_home", "docking", "going_to_wash_the_mop"):
        assert (
            select_resume_command_from_status(
                state=state,
                in_returning=0,
                in_cleaning=0,
            )
            == APP_RESUME_COMMAND
        )


def test_select_resume_command_for_clean_command_maps_start_modes() -> None:
    assert select_resume_command_for_clean_command("app_start") == APP_RESUME_COMMAND
    assert select_resume_command_for_clean_command("app_segment_clean") == APP_RESUME_COMMAND
    assert select_resume_command_for_clean_command("APP_ZONED_CLEAN") == APP_RESUME_COMMAND
    assert select_resume_command_for_clean_command("find_me") is None
