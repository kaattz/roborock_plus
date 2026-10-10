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
    """The timeout must cover the settle *and* still leave time to observe arrival.

    Asserting only `TIMEOUT > MIN_TRAVEL` would accept a value one second above
    the gate, which leaves no room to ever see the door arrive and would time out
    on a door that is opening perfectly well.
    """
    assert (
        MODULE.DEFAULT_GARAGE_DOOR_TIMEOUT - MODULE.DEFAULT_GARAGE_DOOR_MIN_TRAVEL
        >= 30
    ), "the settle must be followed by a real wait, not an instant timeout"


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


# --- Behavioural coverage for the wait ---------------------------------------
#
# The two assertions above are source-text checks, and they are NOT sufficient:
# replacing the real `time.monotonic() - started` with a constant still leaves
# both substrings present (`started = time.monotonic()` supplies `monotonic()`),
# so the wiring of real elapsed time into the predicate was unenforced. Measured
# escaping mutation: `time.monotonic() - started` -> `999`, 10 tests still green.
#
# The tests below drive the real coroutine against a virtual clock instead, so
# the echo cannot release the robot and a constant cannot fake the wait.


class _Clock:
    """A virtual clock standing in for time.monotonic and asyncio.sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []


class _FakeTime:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock.now


class _FakeAsyncio:
    """The subset of asyncio the guard uses, against a virtual clock.

    `sleep` raises TimeoutError once the deadline set by `timeout()` would be
    crossed, which is what real asyncio.timeout achieves by cancelling the task.
    """

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.deadline: float | None = None

    def timeout(self, seconds: float):
        fake = self

        class _Timeout:
            async def __aenter__(self):
                fake.deadline = fake._clock.now + seconds
                return self

            async def __aexit__(self, *exc) -> bool:
                fake.deadline = None
                return False

        return _Timeout()

    async def sleep(self, seconds: float) -> None:
        if self.deadline is not None and self._clock.now + seconds > self.deadline:
            raise TimeoutError
        self._clock.sleeps.append(seconds)
        self._clock.now += seconds


class _State:
    def __init__(self, position) -> None:
        self.attributes = {"current_position": position}


class _Hass:
    """Minimal hass: a cover whose reported position is scripted."""

    def __init__(self, position) -> None:
        self._position = position
        self.service_calls: list[tuple] = []
        outer = self

        class _States:
            def get(self, entity_id):
                return _State(outer._position())

        class _Services:
            async def async_call(self, domain, service, data, blocking=False):
                outer.service_calls.append((domain, service, data))

        self.states = _States()
        self.services = _Services()


def _run_guard(hass: _Hass, clock: _Clock) -> str:
    """Run the real guard coroutine. Returns 'returned' or 'raised'."""
    import asyncio as real_asyncio
    import sys
    import types

    # homeassistant.exceptions is imported lazily inside the guard; the real
    # package is not installed in the test environment.
    if "homeassistant.exceptions" not in sys.modules:
        pkg = types.ModuleType("homeassistant")
        exc = types.ModuleType("homeassistant.exceptions")

        class HomeAssistantError(Exception):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args)

        exc.HomeAssistantError = HomeAssistantError
        pkg.exceptions = exc
        sys.modules.setdefault("homeassistant", pkg)
        sys.modules["homeassistant.exceptions"] = exc

    options = {
        MODULE.CONF_GARAGE_GUARD_ENABLED: True,
        MODULE.CONF_GARAGE_DOOR_ENTITY_ID: "cover.vacuum_garage_door",
    }

    real_time, real_asyncio_mod = MODULE.time, MODULE.asyncio
    MODULE.time = _FakeTime(clock)
    MODULE.asyncio = _FakeAsyncio(clock)
    try:
        real_asyncio.run(MODULE.async_guard_garage_open(hass, options))
    except Exception as err:  # noqa: BLE001 - the timeout path is expected
        return f"raised:{type(err).__name__}"
    finally:
        MODULE.time, MODULE.asyncio = real_time, real_asyncio_mod
    return "returned"


def test_echo_cannot_release_the_robot() -> None:
    """The core safety property, behaviourally.

    The door is shut, so the pre-check declines; the instant open_cover is
    issued the device echoes 100 while the door has not moved. The guard must not
    accept that echo -- it must wait out the whole settle window.
    """
    clock = _Clock()
    hass = _Hass(lambda: 100 if hass.service_calls else 0)

    outcome = _run_guard(hass, clock)

    assert outcome == "returned"
    assert len(hass.service_calls) == 1, "the guard must open the door exactly once"
    assert clock.now >= MODULE.DEFAULT_GARAGE_DOOR_MIN_TRAVEL, (
        f"the guard released the robot after only {clock.now}s of simulated time; "
        "an echoed position must not satisfy the wait"
    )
    assert clock.sleeps, "the guard must actually wait, not fall straight through"


def test_already_open_door_is_not_stalled() -> None:
    """The pre-check must not impose the settle window on an already-open door."""
    clock = _Clock()
    hass = _Hass(lambda: 100)

    outcome = _run_guard(hass, clock)

    assert outcome == "returned"
    assert hass.service_calls == [], "an open door must not be re-commanded"
    assert clock.sleeps == [], "an open door must not wait at all"


def test_obstructed_door_times_out_rather_than_releasing() -> None:
    """Time alone is not enough: a door that never opens must raise."""
    clock = _Clock()
    hass = _Hass(lambda: 20 if hass.service_calls else 0)

    outcome = _run_guard(hass, clock)

    assert outcome == "raised:HomeAssistantError", (
        "a door that echoes nothing and never reaches the position must raise, "
        "not release the robot"
    )
    assert clock.now >= MODULE.DEFAULT_GARAGE_DOOR_MIN_TRAVEL
