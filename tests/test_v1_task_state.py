from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "v1_task_state.py"
)
SPEC = spec_from_file_location("roborock_plus_v1_task_state", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

is_v1_task_active = MODULE.is_v1_task_active


def _state(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def test_v1_task_active_remains_on_when_paused() -> None:
    assert is_v1_task_active(
        state=_state("paused"),
        in_cleaning=False,
        in_returning=False,
    )


def test_v1_task_active_is_on_while_returning() -> None:
    assert is_v1_task_active(
        state=_state("charging"),
        in_cleaning=False,
        in_returning=True,
    )


def test_v1_task_active_is_on_while_mid_task_at_dock() -> None:
    assert is_v1_task_active(
        state=_state("washing_the_mop"),
        in_cleaning=False,
        in_returning=False,
    )


def test_v1_task_active_is_off_when_finished_at_dock() -> None:
    assert not is_v1_task_active(
        state=_state("charging"),
        in_cleaning=False,
        in_returning=False,
    )


def test_v1_task_active_stays_on_while_driving_home_without_the_flag() -> None:
    """`returning_home` with `in_returning` already cleared is still a live task.

    Measured 2026-11-10: `task_active` went off at 15:09:21 while `vacuum` was
    still `returning`, and only reached `docked` at 15:09:26. 2026-11-08 shows
    the same 4 s gap (11:11:05 -> 11:11:14) and an 11 s one (11:32:46 ->
    11:32:57). Any consumer that reads a single sample in that window sees "task
    over, robot not docked" and has to call it a failure.

    `docking` was already in the table; `returning_home` was the one state
    missing, so the whole outbound leg of the drive home depended on the flag.
    """
    assert is_v1_task_active(
        state=_state("returning_home"),
        in_cleaning=False,
        in_returning=False,
    )


def test_v1_task_active_is_off_once_the_robot_is_docked() -> None:
    """The other half of the same window: arrival still ends the task."""
    assert not is_v1_task_active(
        state=_state("charging"),
        in_cleaning=False,
        in_returning=False,
    )
