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


# --- the last-resort backstop ---------------------------------------------
#
# The short window answers "has it stopped moving?". It cannot answer "is it
# moving but getting nowhere", because its anchor resets as soon as the robot
# travels further than `radius` from it. Measured with the real tracker before
# the backstop existed:
#
#     oscillation amplitude  30 -> stuck after 120s
#     oscillation amplitude 190 -> stuck after 120s
#     oscillation amplitude 300 -> NEVER stuck
#
# 300 units is under a second of travel at cleaning speed, so a robot rubbing
# against a door that did not open exceeds the radius on every swing and resets
# the anchor forever. The backstop judges over a much longer window and region.


def _oscillate(tracker, *, amplitude, seconds=1800, step=5, start=0):
    """Rub back and forth within `amplitude`, returning the verdicts.

    ``start`` offsets the clock so consecutive calls continue one timeline rather
    than replaying it -- reusing timestamps would be treated as a repeated sample
    and correctly ignored.
    """
    verdicts = []
    for tick in range(seconds // step):
        t = start + tick * step
        x = 0.0 if tick % 2 == 0 else amplitude
        verdicts.append(_observe(tracker, t, x=x, y=0.0))
    return verdicts


def test_oscillation_beyond_the_short_radius_is_caught_by_the_backstop() -> None:
    """The gap this closes: rubbing wider than the short radius, forever.

    Before the backstop this returned False for every sample, whatever the
    duration -- the short window's anchor resets on each swing.
    """
    tracker = _tracker(window_s=120, radius=200.0)

    verdicts = _oscillate(tracker, amplitude=300.0)

    assert any(verdicts), "a robot rubbing at amplitude 300 must eventually be caught"
    assert tracker.is_backstopped(), (
        "the verdict must be the backstop's; the short window cannot see this"
    )


def test_the_backstop_fires_at_its_window_not_earlier() -> None:
    """It must not fire so early that ordinary work trips it."""
    tracker = _tracker(window_s=120, radius=200.0)
    window_s = int(MODULE.BACKSTOP_WINDOW.total_seconds())

    early = _oscillate(tracker, amplitude=300.0, seconds=window_s - 30)
    assert not any(early), "the backstop must not fire before its window elapses"

    later = _oscillate(tracker, amplitude=300.0, seconds=60, start=window_s - 30)
    assert any(later), "it must fire once the window has elapsed"
    assert tracker.is_backstopped()


def test_genuine_travel_never_trips_the_backstop() -> None:
    """A robot that actually goes somewhere must not be reported stuck.

    900s of cleaning at ~300 units/s covers ~270000 units, so a robot moving in a
    straight line leaves the backstop region almost immediately.
    """
    tracker = _tracker(window_s=120, radius=200.0)

    verdicts = [_observe(tracker, i * 5, x=i * 500.0, y=0.0) for i in range(360)]

    assert not any(verdicts)
    assert not tracker.is_stuck


def test_working_a_room_does_not_trip_the_backstop() -> None:
    """Moving around inside a room must not look like being trapped.

    The robot travels far between samples -- well beyond the backstop radius --
    so its anchor keeps moving with it.
    """
    tracker = _tracker(window_s=120, radius=200.0)

    verdicts = [
        _observe(tracker, i * 5, x=i * 2000.0, y=(i % 3) * 1500.0)
        for i in range(360)
    ]

    assert not any(verdicts)


def test_escaping_the_region_clears_the_backstop_verdict() -> None:
    """It is not stuck any more once it gets out, so the verdict must clear."""
    tracker = _tracker(window_s=120, radius=200.0)

    _oscillate(tracker, amplitude=300.0)
    assert tracker.is_backstopped()

    # Leaves the region for good.
    assert not _observe(tracker, 2000, x=50_000.0)
    assert not tracker.is_stuck
    assert not tracker.is_backstopped()


def test_an_idle_state_clears_the_backstop_too() -> None:
    """A pause or a mop wash means not moving is correct, so both must clear."""
    tracker = _tracker(window_s=120, radius=200.0)

    _oscillate(tracker, amplitude=300.0)
    assert tracker.is_backstopped()

    assert not _observe(tracker, 1900, should_move=False)
    assert not tracker.is_stuck
    assert not tracker.is_backstopped()


def test_the_backstop_radius_exceeds_the_short_one_by_a_wide_margin() -> None:
    """Both numbers are safety margins, so pin their relationship.

    The user can raise the short window's radius to `MAX_V1_STUCK_RADIUS`, so a
    backstop at or below that would measure nothing the short window had not
    already measured -- and once the short radius equals the backstop's, the
    backstop can never fire at all.
    """
    assert MODULE.BACKSTOP_RADIUS > MODULE.MAX_V1_STUCK_RADIUS, (
        "the backstop radius must stay clear of the largest configurable short "
        "radius, or raising the short radius silently disables the backstop"
    )
    assert MODULE.BACKSTOP_WINDOW > timedelta(
        seconds=MODULE.MAX_V1_STUCK_WINDOW
    ), "the backstop must outlast the longest configurable short window"


def test_oscillation_inside_the_short_radius_uses_the_short_window() -> None:
    """The fast path must still work; the backstop is not the only rule."""
    tracker = _tracker(window_s=120, radius=200.0)

    verdicts = _oscillate(tracker, amplitude=100.0, seconds=200)

    assert any(verdicts)
    assert not tracker.is_backstopped(), (
        "a tight oscillation is the short window's job; using the backstop would "
        "delay the report by 15 minutes for no reason"
    )


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


def test_event_payload_carries_the_reason() -> None:
    """A notification has to be able to tell the two verdicts apart.

    "It stopped" sends someone to look at the door; "it has been rubbing against
    something for fifteen minutes" explains why nothing looked wrong. Conflating
    them is what let the gap go unnoticed.
    """
    stopped = MODULE.build_stuck_event_data(
        entity_id=None,
        x=None,
        y=None,
        state="cleaning",
        seconds_stuck=130,
        reason="stopped",
    )
    trapped = MODULE.build_stuck_event_data(
        entity_id=None,
        x=None,
        y=None,
        state="cleaning",
        seconds_stuck=910,
        reason="trapped",
    )

    assert stopped["reason"] == "stopped"
    assert trapped["reason"] == "trapped"


def test_event_payload_omits_the_reason_when_unknown() -> None:
    """An older call site must not emit a null reason an automation would print."""
    data = MODULE.build_stuck_event_data(
        entity_id=None,
        x=None,
        y=None,
        state="cleaning",
        seconds_stuck=10,
    )

    assert "reason" not in data
