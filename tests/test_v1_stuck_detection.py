"""Tests for stuck detection: the whitelist, option clamping and the tracker."""

import sys
from datetime import datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "v1_stuck_detection.py"
)
SPEC = spec_from_file_location("roborock_plus_v1_stuck_detection", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
# dataclasses resolve annotations through sys.modules, so the module has to be
# registered before it is executed.
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

T0 = datetime(2026, 10, 7, 12, 0, 0)


def _tracker(window_s: int = 120, radius: float = 200.0) -> object:
    return MODULE.StuckTracker(window=timedelta(seconds=window_s), radius=radius)


def _observe(tracker, seconds, *, x=0.0, y=0.0, should_move=True, state=None):
    """Feed one observation `seconds` after T0."""
    at = T0 + timedelta(seconds=seconds)
    return tracker.observe(
        now=at,
        sample_time=at,
        x=x,
        y=y,
        should_move=should_move,
    )


# --- whitelist ------------------------------------------------------------


def test_movement_states_cover_cleaning_and_travelling() -> None:
    for state in (
        "cleaning",
        "segment_cleaning",
        "zoned_cleaning",
        "returning_home",
        "docking",
        "going_to_target",
        "going_to_wash_the_mop",
        "manual_mode",
    ):
        assert MODULE.should_assess_movement(state=state), state


def test_legitimately_idle_states_are_not_assessed() -> None:
    """These are the states that must never produce a stuck verdict.

    Every one of them is a state in which the robot is expected to sit still
    while the task is still alive, so treating "not moving" as a fault here
    would stop a working clean.
    """
    for state in (
        "washing_the_mop",
        "emptying_the_bin",
        "charging",
        "paused",
        "idle",
        "error",
        "starting",
        "attaching_the_mop",
        "detaching_the_mop",
        "charger_disconnected",
    ):
        assert not MODULE.should_assess_movement(state=state), state


def test_unknown_states_are_not_assessed() -> None:
    """An unrecognised state fails safe by going unassessed."""
    assert not MODULE.should_assess_movement(state="some_future_state")
    assert not MODULE.should_assess_movement(state=None)


def test_state_enum_like_objects_are_read_by_name() -> None:
    class _State:
        name = "cleaning"

    assert MODULE.should_assess_movement(state=_State())


def test_task_active_states_are_not_reused_here() -> None:
    """`task_active` stays on while paused or washing, so it cannot be the gate."""
    for state in ("paused", "washing_the_mop"):
        assert not MODULE.should_assess_movement(state=state)


# --- options --------------------------------------------------------------


def test_options_default_when_absent() -> None:
    options = MODULE.resolve_stuck_options(None)

    assert options.enabled is True
    assert options.window == timedelta(seconds=MODULE.DEFAULT_V1_STUCK_WINDOW)
    assert options.radius == float(MODULE.DEFAULT_V1_STUCK_RADIUS)


def test_options_can_disable_detection() -> None:
    options = MODULE.resolve_stuck_options({"v1_stuck_detection_enabled": False})

    assert options.enabled is False


def test_options_window_is_clamped() -> None:
    low = MODULE.resolve_stuck_options({"v1_stuck_window": 1})
    high = MODULE.resolve_stuck_options({"v1_stuck_window": 99999})

    assert low.window == timedelta(seconds=MODULE.MIN_V1_STUCK_WINDOW)
    assert high.window == timedelta(seconds=MODULE.MAX_V1_STUCK_WINDOW)


def test_options_radius_is_clamped() -> None:
    low = MODULE.resolve_stuck_options({"v1_stuck_radius": 1})
    high = MODULE.resolve_stuck_options({"v1_stuck_radius": 99999})

    assert low.radius == float(MODULE.MIN_V1_STUCK_RADIUS)
    assert high.radius == float(MODULE.MAX_V1_STUCK_RADIUS)


def test_options_fall_back_on_garbage() -> None:
    for value in ("abc", None, [], {}):
        options = MODULE.resolve_stuck_options({"v1_stuck_window": value})
        assert options.window == timedelta(seconds=MODULE.DEFAULT_V1_STUCK_WINDOW), value


def test_options_treat_booleans_as_garbage() -> None:
    """`True` is an int in Python; it must not become a 1-second window."""
    options = MODULE.resolve_stuck_options({"v1_stuck_window": True})
    assert options.window == timedelta(seconds=MODULE.DEFAULT_V1_STUCK_WINDOW)


# --- tracker --------------------------------------------------------------


def test_moving_robot_is_never_stuck() -> None:
    tracker = _tracker()

    for step in range(40):
        assert not _observe(tracker, step * 5, x=step * 500.0, y=0.0)


def test_stationary_robot_becomes_stuck_after_the_window() -> None:
    tracker = _tracker(window_s=120)

    assert not _observe(tracker, 0)
    assert not _observe(tracker, 60)
    assert not _observe(tracker, 119)
    assert _observe(tracker, 120)


def test_stuck_clears_once_the_robot_moves() -> None:
    tracker = _tracker(window_s=120)

    _observe(tracker, 0)
    assert _observe(tracker, 130)
    assert tracker.is_stuck

    # Moves well beyond the radius.
    assert not _observe(tracker, 140, x=5000.0)
    assert not tracker.is_stuck


def test_jitter_within_the_radius_is_still_stuck() -> None:
    """Two samples taken while parked differ by a unit or two.

    Equality comparison would read that jitter as movement; the radius has to
    absorb it. The window is not elapsed until the last observation, which is
    where the verdict appears.
    """
    tracker = _tracker(window_s=120, radius=200.0)

    _observe(tracker, 0, x=25728.0, y=24233.0)
    assert not _observe(tracker, 60, x=25727.0, y=24232.0)
    assert _observe(tracker, 130, x=25729.0, y=24231.0)


def test_movement_beyond_the_radius_restarts_the_window() -> None:
    tracker = _tracker(window_s=120, radius=200.0)

    _observe(tracker, 0)
    # Moved 300 units at t=100: a new window starts here.
    assert not _observe(tracker, 100, x=300.0)
    # 100s later is still short of the window measured from the new anchor.
    assert not _observe(tracker, 200, x=300.0)
    assert _observe(tracker, 220, x=300.0)


def test_idle_state_clears_a_stuck_verdict() -> None:
    """A mop wash or a pause means not moving is correct, so it must clear."""
    tracker = _tracker(window_s=120)

    _observe(tracker, 0)
    assert _observe(tracker, 130)
    assert tracker.is_stuck

    assert not _observe(tracker, 140, should_move=False)
    assert not tracker.is_stuck


def test_missing_position_does_not_invent_a_verdict() -> None:
    tracker = _tracker(window_s=120)

    assert not _observe(tracker, 0, x=None, y=None)
    assert not _observe(tracker, 300, x=None, y=None)
    assert not tracker.is_stuck


def test_missing_position_keeps_an_existing_verdict() -> None:
    """The robot has not been seen to move, so the verdict stands."""
    tracker = _tracker(window_s=120)

    _observe(tracker, 0)
    assert _observe(tracker, 130)

    assert _observe(tracker, 140, x=None, y=None)
    assert tracker.is_stuck


def test_repeated_sample_time_is_not_new_evidence() -> None:
    """A failed refresh leaves the old position in place, with the old time.

    Counting that as a fresh "still not moving" reading would let a broken map
    read turn into a stuck verdict.
    """
    tracker = _tracker(window_s=120)

    tracker.observe(now=T0, sample_time=T0, x=0.0, y=0.0, should_move=True)
    # Same sample replayed much later: must not accumulate time.
    assert not tracker.observe(
        now=T0 + timedelta(seconds=600),
        sample_time=T0,
        x=0.0,
        y=0.0,
        should_move=True,
    )
    assert not tracker.is_stuck


def test_seconds_stuck_counts_from_the_start_of_the_window() -> None:
    tracker = _tracker(window_s=120)

    _observe(tracker, 0)
    assert _observe(tracker, 130)

    assert tracker.seconds_stuck(T0 + timedelta(seconds=130)) == 130.0


def test_seconds_stuck_is_none_when_not_stuck() -> None:
    tracker = _tracker()

    _observe(tracker, 0)
    assert tracker.seconds_stuck(T0) is None


def test_reset_clears_everything() -> None:
    tracker = _tracker(window_s=120)

    _observe(tracker, 0)
    assert _observe(tracker, 130)

    tracker.reset()

    assert not tracker.is_stuck
    assert tracker.seconds_stuck(T0 + timedelta(seconds=200)) is None


# --- event payload --------------------------------------------------------


def test_event_name_is_namespaced() -> None:
    assert MODULE.EVENT_VACUUM_STUCK == "roborock_plus_vacuum_stuck"


def test_event_payload_carries_what_an_automation_needs() -> None:
    data = MODULE.build_stuck_event_data(
        entity_id="vacuum.g20s_ultra",
        x=25728.0,
        y=24233.0,
        state="returning_home",
        seconds_stuck=132.7,
        entry_id="abc123",
    )

    assert data["entity_id"] == "vacuum.g20s_ultra"
    assert data["x"] == 25728.0
    assert data["y"] == 24233.0
    assert data["state"] == "returning_home"
    assert data["seconds_stuck"] == 132
    assert data["entry_id"] == "abc123"


def test_event_payload_omits_unknown_identifiers() -> None:
    data = MODULE.build_stuck_event_data(
        entity_id=None,
        x=None,
        y=None,
        state="cleaning",
        seconds_stuck=None,
    )

    assert "entity_id" not in data
    assert "entry_id" not in data
    assert "x" not in data
    assert "y" not in data
    assert data["state"] == "cleaning"
    assert data["seconds_stuck"] is None
