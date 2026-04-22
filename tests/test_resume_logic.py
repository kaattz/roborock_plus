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
select_start_or_resume_command = MODULE.select_start_or_resume_command


def test_select_start_or_resume_command_for_returning() -> None:
    assert (
        select_start_or_resume_command(in_returning=1, in_cleaning=None)
        == "app_charge"
    )


def test_select_start_or_resume_command_for_global_clean_resume() -> None:
    assert (
        select_start_or_resume_command(in_returning=None, in_cleaning=1)
        == APP_RESUME_COMMAND
    )


def test_select_start_or_resume_command_for_zoned_clean_resume() -> None:
    assert (
        select_start_or_resume_command(in_returning=None, in_cleaning=2)
        == "resume_zoned_clean"
    )


def test_select_start_or_resume_command_for_segment_clean_resume() -> None:
    assert (
        select_start_or_resume_command(in_returning=None, in_cleaning=3)
        == "resume_segment_clean"
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
