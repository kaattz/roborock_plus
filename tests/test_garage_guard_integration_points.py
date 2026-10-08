"""Every entry point that can move the robot must open the door first.

The garage guard is only useful if it is actually on the path. It was not:
`vacuum.clean_spot` called `send` directly, so a spot clean started with the
cabinet door still shut. The earlier version of this file asserted only that the
identifiers appear *somewhere* in `vacuum.py`, which stayed true the whole time
`clean_spot` bypassed the guard.

So these tests check the structural property instead: for every command that
moves the robot out of its dock, the guard call must appear immediately before
the `send` that issues it.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "roborock_plus"
VACUUM = COMPONENT / "vacuum.py"
BUTTON = COMPONENT / "button.py"
GUARD = COMPONENT / "garage_guard.py"

# Commands that put the robot on the floor. Anything here must be guarded.
DISPATCHING_COMMANDS = (
    "APP_GOTO_TARGET",
    "APP_SEGMENT_CLEAN",
    "APP_SPOT",
    "APP_START",
    "APP_ZONED_CLEAN",
)


def _vacuum_module() -> ast.Module:
    return ast.parse(VACUUM.read_text(encoding="utf-8"))


def _method_bodies(name: str) -> list[str]:
    """Return the source of every method with this name in vacuum.py.

    There are several vacuum classes in the file (V1, A01, Q10); each defines
    `async_start`, so all of them are collected rather than the first match.
    """
    source = VACUUM.read_text(encoding="utf-8")
    found = []
    for node in ast.walk(_vacuum_module()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            found.append(segment)
    return found


def _guard_commands() -> set[str]:
    """Read the set of guarded commands out of the shipped module."""
    source = GUARD.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", None) == "_GUARDED_CLEAN_COMMANDS":
                    return set(ast.literal_eval(node.value))
    raise AssertionError("_GUARDED_CLEAN_COMMANDS not found")


class TestGuardedCommandSet:
    def test_every_dispatching_command_is_guarded(self) -> None:
        guarded = _guard_commands()
        for command in DISPATCHING_COMMANDS:
            assert command in guarded or command.lower() in guarded, (
                f"{command} moves the robot out of its dock but is not in "
                "_GUARDED_CLEAN_COMMANDS, so it can be issued into a shut door"
            )

    def test_pause_and_stop_are_not_guarded(self) -> None:
        """They do not send the robot out, and guarding them would block them
        behind a door operation at exactly the moment they are needed."""
        guarded = _guard_commands()
        for command in ("APP_PAUSE", "APP_STOP", "APP_CHARGE"):
            assert command not in guarded
            assert command.lower() not in guarded


class TestSpotCleanIsGuarded:
    """The entry point that was missed."""

    def test_clean_spot_guards_before_sending(self) -> None:
        bodies = _method_bodies("async_clean_spot")
        assert bodies, "async_clean_spot not found in vacuum.py"
        for body in bodies:
            assert "_async_guard_clean_command" in body, (
                "spot clean sends the robot out of its dock and must open the "
                "door first"
            )
            assert body.index("_async_guard_clean_command") < body.index(
                "await self.send("
            ), "the guard must run before the command is sent, not after"

    def test_clean_spot_is_watched_for_a_failed_start(self) -> None:
        """A routine-style command can be accepted and never start."""
        for body in _method_bodies("async_clean_spot"):
            assert "_watch_clean_command" in body


class TestGotoIsGuarded:
    """Unused here today, but it moves the robot, so it must be guarded."""

    def test_goto_position_guards_before_sending(self) -> None:
        # Only the V1 class implements goto; the other vacuum classes raise
        # ServiceNotSupported, so the guarded body is the one that sends.
        sending = [
            body
            for body in _method_bodies("async_set_vacuum_goto_position")
            if "await self.send(" in body
        ]
        assert sending, "no implementation of async_set_vacuum_goto_position sends"
        for body in sending:
            assert "_async_guard_clean_command" in body, (
                "goto drives the robot out of its dock and must open the door "
                "first"
            )
            assert body.index("_async_guard_clean_command") < body.index(
                "await self.send("
            )


class TestEveryDispatchSiteIsGuarded:
    """A structural sweep, so a future entry point cannot quietly skip it."""

    def test_no_method_sends_a_dispatching_command_without_the_guard(self) -> None:
        source = VACUUM.read_text(encoding="utf-8")
        unguarded: list[str] = []

        for node in ast.walk(_vacuum_module()):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            body = ast.get_source_segment(source, node) or ""
            for command in DISPATCHING_COMMANDS:
                if f"self.send(RoborockCommand.{command}" not in body:
                    continue
                if "_async_guard_clean_command" not in body:
                    unguarded.append(f"{node.name} -> {command}")

        assert not unguarded, (
            "these methods dispatch a robot-moving command without the guard: "
            + ", ".join(unguarded)
        )


class TestRoutineButtonsAreGuarded:
    def test_button_press_guards_before_running_the_routine(self) -> None:
        source = BUTTON.read_text(encoding="utf-8")
        assert "async_guard_garage_open" in source
        assert source.index("async_guard_garage_open") < source.index(
            "execute_routines"
        ), "the door must be open before the routine is started"

    def test_button_press_watches_the_routine(self) -> None:
        """A routine runs server-side, so acceptance does not mean it started."""
        source = BUTTON.read_text(encoding="utf-8")
        assert "async_watch_clean_command_started" in source
