"""The echo window is one number, shared by every consumer of door position.

Measured on 2026-10-10: a full travel takes 35 seconds (user, stopwatch), while
the device echoes the target position within 0.5 seconds of any motor command.
Anything that reads `current_position` sooner than one travel is reading the
echo, not the door.
"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "door_timing.py"
)
SPEC = spec_from_file_location("roborock_plus_door_timing", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_travel_is_the_measured_35_seconds() -> None:
    """An owner's stopwatch reading; changing it must be a deliberate re-measure."""
    assert MODULE.DOOR_TRAVEL_SECONDS == 35


def test_echo_settle_exceeds_one_full_travel_with_margin() -> None:
    """A settling window shorter than a travel reads the echo, not the door."""
    assert MODULE.ECHO_SETTLE_SECONDS > MODULE.DOOR_TRAVEL_SECONDS
    assert MODULE.ECHO_SETTLE_SECONDS - MODULE.DOOR_TRAVEL_SECONDS >= 10


def test_echo_settle_stays_cheap_enough_to_wait_per_command() -> None:
    """The window is a tradeoff: long enough to outlast the echo, no longer.

    Every consumer waits this long per command (the garage guard before it
    releases the robot, the state machine's door waits, the auto-unlock before
    it pauses the door). Only the safety side used to be bounded, so a value of
    an hour would have passed the suite while making the door unusable.
    """
    assert MODULE.ECHO_SETTLE_SECONDS <= MODULE.DOOR_TRAVEL_SECONDS * 2
