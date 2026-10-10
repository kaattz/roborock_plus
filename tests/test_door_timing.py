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
    assert MODULE.DOOR_TRAVEL_SECONDS == 35


def test_echo_settle_exceeds_one_full_travel_with_margin() -> None:
    """A settling window shorter than a travel reads the echo, not the door."""
    assert MODULE.ECHO_SETTLE_SECONDS > MODULE.DOOR_TRAVEL_SECONDS
    assert MODULE.ECHO_SETTLE_SECONDS - MODULE.DOOR_TRAVEL_SECONDS >= 10


def test_settle_covers_a_slower_than_measured_travel() -> None:
    """Cold weather or added resistance makes the door slower, not faster."""
    assert MODULE.ECHO_SETTLE_SECONDS >= MODULE.DOOR_TRAVEL_SECONDS * 1.25
