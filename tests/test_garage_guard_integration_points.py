from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VACUUM = ROOT / "custom_components" / "roborock_plus" / "vacuum.py"
BUTTON = ROOT / "custom_components" / "roborock_plus" / "button.py"


def test_v1_vacuum_start_like_commands_use_garage_guard() -> None:
    vacuum = VACUUM.read_text(encoding="utf-8")

    assert "async_guard_garage_open" in vacuum
    assert "should_guard_clean_command" in vacuum


def test_routine_buttons_use_garage_guard() -> None:
    button = BUTTON.read_text(encoding="utf-8")

    assert "async_guard_garage_open" in button
    assert "async_press" in button
