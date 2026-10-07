"""Tests for the clean-command watch that reports commands that never start."""

import asyncio
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "clean_command_watch.py"
)
SPEC = spec_from_file_location("roborock_plus_clean_command_watch", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _Clock:
    """Deterministic stand-in for time.monotonic and asyncio.sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _run_watch(*, timeout: float, states: list[bool | None]) -> tuple[bool, _Clock]:
    """Drive async_watch_until_task_starts with a scripted state sequence.

    The final scripted state repeats once the sequence is exhausted, so a run
    that never starts keeps reporting the same idle state until the timeout.
    """
    clock = _Clock()
    remaining = list(states)

    def is_task_active() -> bool | None:
        if len(remaining) > 1:
            return remaining.pop(0)
        return remaining[0]

    started = asyncio.run(
        MODULE.async_watch_until_task_starts(
            timeout=timeout,
            is_task_active=is_task_active,
            poll_interval=5.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
    )
    return started, clock


# --- resolve_clean_command_watch_timeout -----------------------------------


def test_timeout_defaults_when_no_options() -> None:
    assert (
        MODULE.resolve_clean_command_watch_timeout(None)
        == MODULE.DEFAULT_CLEAN_COMMAND_WATCH_TIMEOUT
    )
    assert (
        MODULE.resolve_clean_command_watch_timeout({})
        == MODULE.DEFAULT_CLEAN_COMMAND_WATCH_TIMEOUT
    )


def test_timeout_zero_disables_the_watch() -> None:
    assert MODULE.resolve_clean_command_watch_timeout({"clean_command_watch_timeout": 0}) == 0


def test_timeout_is_clamped_to_the_supported_range() -> None:
    options = {"clean_command_watch_timeout": 1}
    assert (
        MODULE.resolve_clean_command_watch_timeout(options)
        == MODULE.MIN_CLEAN_COMMAND_WATCH_TIMEOUT
    )

    options = {"clean_command_watch_timeout": 99999}
    assert (
        MODULE.resolve_clean_command_watch_timeout(options)
        == MODULE.MAX_CLEAN_COMMAND_WATCH_TIMEOUT
    )


def test_timeout_falls_back_to_default_on_garbage() -> None:
    for value in ("abc", None, [], {}, True, False):
        assert (
            MODULE.resolve_clean_command_watch_timeout(
                {"clean_command_watch_timeout": value}
            )
            == MODULE.DEFAULT_CLEAN_COMMAND_WATCH_TIMEOUT
        ), value


def test_timeout_accepts_a_float() -> None:
    assert MODULE.resolve_clean_command_watch_timeout({"clean_command_watch_timeout": 45.7}) == 45


# --- async_watch_until_task_starts ----------------------------------------


def test_reports_started_when_a_task_appears() -> None:
    started, _ = _run_watch(timeout=60, states=[False, False, True])

    assert started is True


def test_reports_not_started_when_idle_for_the_whole_window() -> None:
    started, clock = _run_watch(timeout=60, states=[False])

    assert started is False
    # The window is respected: it stops at the deadline rather than looping on.
    assert clock.now == 60


def test_an_unreadable_state_is_not_reported_as_a_failure() -> None:
    """A state that can never be read is the integration's problem, not this command's.

    Reporting it would raise a false alarm whenever the coordinator happens to
    have no data yet.
    """
    started, _ = _run_watch(timeout=60, states=[None])

    assert started is True


def test_a_readable_idle_state_still_reports_after_an_unreadable_one() -> None:
    started, _ = _run_watch(timeout=60, states=[None, False])

    assert started is False


def test_a_zero_timeout_returns_immediately_without_polling() -> None:
    started, clock = _run_watch(timeout=0, states=[False])

    assert started is True
    assert clock.slept == []


def test_watch_stops_polling_once_the_task_starts() -> None:
    _, clock = _run_watch(timeout=600, states=[False, False, True])

    # Two idle polls, then the poll that observed the task.
    assert clock.now == 10


# --- event payload ---------------------------------------------------------


def test_event_name_is_namespaced() -> None:
    assert MODULE.EVENT_CLEAN_COMMAND_NOT_STARTED == "roborock_plus_clean_command_not_started"


def test_payload_carries_what_an_automation_needs() -> None:
    data = MODULE.build_clean_command_not_started_data(
        entity_id="vacuum.g20s_ultra",
        command="execute_scene",
        timeout=60,
        entry_id="abc123",
    )

    assert data["command"] == "execute_scene"
    assert data["timeout"] == 60
    assert data["entity_id"] == "vacuum.g20s_ultra"
    assert data["entry_id"] == "abc123"


def test_payload_omits_unknown_identifiers() -> None:
    data = MODULE.build_clean_command_not_started_data(
        entity_id=None,
        command="app_start",
        timeout=60,
    )

    assert "entity_id" not in data
    assert "entry_id" not in data
    assert data["command"] == "app_start"


# --- safety property ------------------------------------------------------


def test_the_watch_never_closes_the_door() -> None:
    """Only reporting is allowed here.

    A failed command does not prove the vacuum stayed put, and the door is
    shared with other uses, so closing it could trap the robot or shut someone
    in. This is the invariant that keeps that from creeping in later.
    """
    source = MODULE_PATH.read_text(encoding="utf-8")

    assert "close_cover" not in source
    assert "async_call" not in source
