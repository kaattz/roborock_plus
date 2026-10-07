"""Guard the wiring that reports clean commands which never start.

These are source-level assertions because Home Assistant is not importable in
this environment. They exist to catch the watch being dropped from an entry
point: the gap only shows up when a routine fails on real hardware, which is
exactly when it is hardest to notice.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "roborock_plus"
VACUUM = COMPONENT / "vacuum.py"
BUTTON = COMPONENT / "button.py"


def test_routine_buttons_watch_the_command_they_start() -> None:
    """The routine button is the entry point that failed in practice.

    A routine is executed asynchronously on the server, so execute_routines
    returning without error says nothing about whether the vacuum moved.
    """
    button = BUTTON.read_text(encoding="utf-8")
    press = button.split("class RoborockRoutineButtonEntity", 1)[1].split(
        "class RoborockButtonEntityA01", 1
    )[0]

    assert "execute_routines" in press
    assert "async_watch_clean_command_started" in press
    # The watch has to run after the routine is requested.
    assert press.index("execute_routines") < press.index(
        "async_watch_clean_command_started"
    )


def test_vacuum_watch_runs_after_the_command_is_sent() -> None:
    """Watching before sending would race the command itself."""
    vacuum = VACUUM.read_text(encoding="utf-8")

    assert "def _watch_clean_command" in vacuum
    for method in ("async_start", "async_clean_segments", "async_send_command"):
        body = vacuum.split(f"async def {method}", 1)[1].split("\n    async def ", 1)[0]
        assert "_watch_clean_command" in body, method
        assert body.index("self.send") < body.index("_watch_clean_command"), method


def test_only_guarded_commands_are_watched() -> None:
    """A pause or a locate must not be reported as a failed clean.

    Gating the watch on the same predicate as the door guard keeps the two in
    step: anything that can move the robot out of the dock is watched, and
    nothing else is.
    """
    vacuum = VACUUM.read_text(encoding="utf-8")
    watch = vacuum.split("def _watch_clean_command", 1)[1].split(
        "async def _async_guard_clean_command", 1
    )[0]

    assert "should_guard_clean_command(command)" in watch


def test_clean_command_watch_is_wired_into_the_options_flow() -> None:
    flow = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")

    assert "CONF_CLEAN_COMMAND_WATCH_TIMEOUT" in flow
    assert "DEFAULT_CLEAN_COMMAND_WATCH_TIMEOUT" in flow
