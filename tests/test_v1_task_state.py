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
