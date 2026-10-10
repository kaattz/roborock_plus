from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "garage_guard.py"
)
SPEC = spec_from_file_location("roborock_plus_garage_guard", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_guard_handles_start_segment_and_zoned_clean_commands() -> None:
    assert MODULE.should_guard_clean_command("app_start")
    assert MODULE.should_guard_clean_command("app_segment_clean")
    assert MODULE.should_guard_clean_command("app_zoned_clean")
    assert MODULE.should_guard_clean_command("APP_START")
    assert MODULE.should_guard_clean_command("APP_SEGMENT_CLEAN")
    assert MODULE.should_guard_clean_command("APP_ZONED_CLEAN")


def test_guard_ignores_non_start_commands() -> None:
    assert not MODULE.should_guard_clean_command("app_pause")
    assert not MODULE.should_guard_clean_command("find_me")


def test_cover_position_at_least_95_is_open_enough() -> None:
    assert not MODULE.is_garage_door_open_enough(None)
    assert not MODULE.is_garage_door_open_enough(94.9)
    assert MODULE.is_garage_door_open_enough(95)
    assert MODULE.is_garage_door_open_enough(100)


def test_garage_guard_requires_only_enabled_and_cover() -> None:
    assert not MODULE.is_garage_guard_ready({})
    assert MODULE.is_garage_guard_ready(
        {
            MODULE.CONF_GARAGE_GUARD_ENABLED: True,
            MODULE.CONF_GARAGE_DOOR_ENTITY_ID: "cover.vacuum_garage_door",
        }
    )


def _door_timing():
    """Load the timing constants by path; the package itself cannot be imported."""
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "roborock_plus"
        / "door_timing.py"
    )
    spec = spec_from_file_location("roborock_plus_door_timing_for_guard", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_min_travel_outlasts_the_echo_window() -> None:
    """The guard must not accept a position read during the echo."""
    assert MODULE.DEFAULT_GARAGE_DOOR_MIN_TRAVEL >= _door_timing().ECHO_SETTLE_SECONDS


def test_timeout_leaves_room_for_the_settle_plus_the_wait() -> None:
    """A timeout shorter than travel + wait would fail a door that is opening fine."""
    assert MODULE.DEFAULT_GARAGE_DOOR_TIMEOUT > MODULE.DEFAULT_GARAGE_DOOR_MIN_TRAVEL


def test_elapsed_time_alone_is_not_enough() -> None:
    """Waiting is necessary but not sufficient: the position must also be high."""
    assert MODULE.door_reached_open_position(elapsed=999, position=10) is False
    assert MODULE.door_reached_open_position(elapsed=999, position=95) is True


def test_position_alone_is_not_enough() -> None:
    """A high position read too early is the echo of the command."""
    assert MODULE.door_reached_open_position(elapsed=0, position=100) is False
    assert MODULE.door_reached_open_position(elapsed=10, position=100) is False


def _guard_body() -> str:
    """Return the body of async_guard_garage_open, up to the next top-level def."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    body = source.split("async def async_guard_garage_open", 1)[1]
    return body.split("\ndef ", 1)[0]


def test_precheck_reads_position_without_waiting() -> None:
    """An already-open door must not be stalled behind the settle window.

    The pre-command check runs before any command is issued, so its reading is
    not an echo. If the elapsed-time gate were applied here, every clean start
    with the door already open would idle for 45 seconds.
    """
    precheck = _guard_body().split("await hass.services.async_call", 1)[0]

    assert "door_reached_open_position" not in precheck, (
        "the pre-command check must not apply the elapsed-time gate: no command "
        "has been issued yet, so there is no echo to outlast"
    )
    assert "elapsed" not in precheck and "monotonic" not in precheck, (
        "the pre-command check must not measure time"
    )
    assert "position" in precheck, (
        "the pre-command check must still read the door position"
    )


def test_postcommand_wait_applies_the_time_gate() -> None:
    """The wait after the command must require elapsed time as well as position."""
    post = _guard_body().split("await hass.services.async_call", 1)[1]

    assert "door_reached_open_position" in post, (
        "the post-command wait must use the elapsed-time gate, or the device's "
        "echo of open_cover satisfies it in half a second"
    )
    assert "monotonic()" in post, "the wait must measure real elapsed time"
