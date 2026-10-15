from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "custom_components" / "roborock_plus" / "garage_guard.py"
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


def test_precheck_does_not_wait_out_the_settle() -> None:
    """An already-open, settled door must not be stalled behind the settle.

    The pre-command check runs before *this* call issues a command, so it must not
    simply sleep for the travel window -- that would stall every clean behind 45
    seconds for no safety gain.

    It does read the clock now, which the earlier version of this test forbade. The
    reading is what makes it safe: a position that changed because another
    automation just commanded the door is an echo, and time is the only thing that
    distinguishes "still open from before" from "echoing a fresh command". The
    behavioural tests below are the real guard; this only pins that the check
    decides rather than sleeps.
    """
    precheck = _guard_body().split("await hass.services.async_call", 1)[0]

    assert "door_reached_open_position" not in precheck, (
        "the pre-command check must not apply the post-command elapsed gate as a "
        "blanket wait; it decides from the reading's age instead"
    )
    assert "sleep" not in precheck, (
        "the pre-command check must not wait at all when the door is already open"
    )
    assert "trusts" in precheck, (
        "the pre-command check must go through the echo window, which compares the "
        "reading's age against the last command for this door"
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

    def __init__(self, epoch_offset: float = 0.0) -> None:
        self.now = 0.0
        self.epoch_offset = epoch_offset
        self.sleeps: list[float] = []


class _FakeTime:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock.now

    def time(self) -> float:
        return self._clock.now + self._clock.epoch_offset


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
    def __init__(self, position, updated_at: float = 0.0) -> None:
        self.attributes = {"current_position": position}
        # HA exposes the epoch seconds of the last real change. The guard reads it
        # to tell a settled reading from a fresh echo of someone else's command.
        self.last_updated_timestamp = updated_at


class _Hass:
    """Minimal hass: a cover whose reported position is scripted."""

    def __init__(self, position, updated_at=0.0) -> None:
        self._position = position
        self._updated_at = updated_at
        self.service_calls: list[tuple] = []
        # Commands issued by something other than this guard, e.g. the state
        # machine. Kept separate from `service_calls` so a test can assert on what
        # the guard itself did without counting the scenario's setup.
        self.external_commands: list[str] = []
        outer = self

        class _States:
            def get(self, entity_id):
                at = outer._updated_at
                return _State(
                    outer._position(),
                    at() if callable(at) else at,
                )

        class _Services:
            async def async_call(self, domain, service, data, blocking=False):
                outer.service_calls.append((domain, service, data))

        self.states = _States()
        self.services = _Services()


def _run_guard(hass: _Hass, clock: _Clock, echo_window=None) -> str:
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
        real_asyncio.run(
            MODULE.async_guard_garage_open(hass, options, echo_window)
        )
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


# --- The pre-command check must not trust someone else's echo -----------------
#
# The check used to return early whenever it read a high position, reasoning that
# "this call issued no command, so the reading is not an echo". That is about the
# call, not the device. Measured path: the state machine's phase C issues
# `open_cover`, the device writes 100 into `current-position` while the door is
# physically at 5%, and a clean command arrives inside the travel window -- the
# guard read 100, returned without calling a single service, and released the
# robot at a nearly-closed door.
#
# The trace below reproduces that ordering honestly: the position jumps the
# instant another automation's command lands, exactly as the device behaves.


def _other_automation_opens(hass: "_Hass", clock: "_Clock", at: float) -> None:
    """Model phase C: a command from elsewhere, and the echo that follows it."""
    hass.external_commands.append("open_cover")
    hass._updated_at = lambda: at  # the device echoes; the entity changes now


def test_another_automations_echo_does_not_release_the_robot() -> None:
    """The gap this closes: a fresh echo read by a later, unrelated command.

    The door is physically nearly shut, but a command another automation issued a
    moment ago makes the device report 100. The guard must not treat that as an
    open door and wave the robot through.
    """
    clock = _Clock()
    # The door reads 100 because of the *other* automation's echo; its entity was
    # updated at t=5, and this guard call happens at t=10 -- inside the window.
    hass = _Hass(lambda: 100, updated_at=lambda: 5.0)
    clock.now = 10.0

    window = MODULE.DoorEchoWindow()
    window.note_command(5.0)

    outcome = _run_guard(hass, clock, window)

    assert outcome == "returned"
    assert len(hass.service_calls) == 1, (
        "the guard must issue its own open_cover rather than trusting the "
        "position another automation's command produced"
    )
    assert clock.now >= MODULE.DEFAULT_GARAGE_DOOR_MIN_TRAVEL, (
        "the guard released the robot after only "
        f"{clock.now - 10.0}s of simulated time; the echo of a command it did not "
        "issue must not satisfy the check"
    )


def test_a_settled_open_door_is_still_not_stalled() -> None:
    """The fix must not impose a wait on a door that is genuinely, stably open.

    Without this the previous test could be satisfied by simply always waiting,
    which would stall every clean behind 45 seconds for no safety gain.
    """
    clock = _Clock()
    # Position 100, entity last changed at t=0, and a command was seen long ago.
    hass = _Hass(lambda: 100, updated_at=lambda: 0.0)
    clock.now = 500.0

    window = MODULE.DoorEchoWindow()
    window.note_command(10.0)

    outcome = _run_guard(hass, clock, window)

    assert outcome == "returned"
    assert hass.service_calls == [], "a settled open door must not be re-commanded"
    assert clock.sleeps == [], (
        "a door that was already open and stable must not wait at all"
    )


# --- DoorEchoWindow, the rule on its own -------------------------------------


def test_no_command_seen_trusts_a_high_position() -> None:
    """Fresh start-up with no observed command: nothing to distrust."""
    window = MODULE.DoorEchoWindow()
    assert window.trusts(now=100.0, position=100, changed_at=99.0) is True
    assert window.trusts(now=100.0, position=10, changed_at=99.0) is False


def test_recent_command_requires_a_settled_reading() -> None:
    window = MODULE.DoorEchoWindow()
    window.note_command(100.0)
    settle = window.SETTLE

    # Changed at the same moment as the command -> that change IS the echo.
    assert (
        window.trusts(now=100.0 + settle - 1, position=100, changed_at=100.0)
        is False
    )
    # Changed long before the command -> the reading predates it, so it is real.
    assert (
        window.trusts(now=100.0 + 1, position=100, changed_at=100.0 - settle)
        is True
    )


def test_old_command_needs_no_stability_check() -> None:
    """Once a full travel has passed, the device has finished; trust the value."""
    window = MODULE.DoorEchoWindow()
    window.note_command(0.0)
    assert window.trusts(now=window.SETTLE + 1, position=100, changed_at=10.0) is True


def test_an_unreadable_change_time_is_unsafe() -> None:
    """Never release a robot because a timestamp could not be read."""
    window = MODULE.DoorEchoWindow()
    window.note_command(100.0)
    assert window.trusts(now=101.0, position=100, changed_at=None) is False


def test_a_low_position_is_never_trusted() -> None:
    """Even with a settled, old reading, a shut door is not open enough."""
    window = MODULE.DoorEchoWindow()
    assert window.trusts(now=10_000.0, position=10, changed_at=0.0) is False
    window.note_command(0.0)
    assert window.trusts(now=10_000.0, position=10, changed_at=0.0) is False


def test_the_guard_records_its_own_command() -> None:
    """The window is only useful if the guard tells it a command happened.

    Without this the window keeps taking its "no command since start-up" path, so
    the very next clean command would trust whatever position it reads -- the gap
    reopened with all the plumbing apparently in place. The behavioural test above
    cannot catch that: it supplies a window it has already primed by hand.
    """
    precheck = _guard_body().split("await hass.services.async_call", 1)[0]
    assert "note_command" in precheck, (
        "the guard must record its own command, or the echo window never learns "
        "that a travel window is open"
    )


def test_the_guard_records_before_it_commands() -> None:
    """Order matters: recording after the call would leave the wait unguarded.

    The position is echoed within ~0.5s of the command, so a window primed after
    the service call has already missed the moment the echo started.
    """
    body = _guard_body()
    note_at = body.find("note_command")
    call_at = body.find("await hass.services.async_call")
    assert note_at != -1 and call_at != -1
    assert note_at < call_at, "the command must be recorded before it is issued"


def test_the_configured_window_reaches_the_guard() -> None:
    """The guard must be given the caller's window, not a fresh one each call.

    A per-call window cannot see a command issued by another automation, which is
    the entire reason the window exists.
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "roborock_plus"
        / "vacuum.py"
    ).read_text(encoding="utf-8")
    call = source.split("async_guard_garage_open(", 1)[1].split(")", 1)[0]
    assert "garage_door_echo_window" in call, (
        "the guard is called without the shared echo window, so it cannot see "
        "commands issued by anything else"
    )


def test_real_epoch_change_time_is_translated_to_monotonic_domain(monkeypatch) -> None:
    clock = _Clock(epoch_offset=1_700_000_000)
    clock.now = 100
    monkeypatch.setattr(MODULE, "time", _FakeTime(clock))
    hass = _Hass(lambda: 100, updated_at=1_700_000_010)
    assert MODULE._configured_cover_changed_at(hass, "cover.door") == 10


def test_recent_command_can_use_settled_epoch_reading_without_extra_travel_wait() -> None:
    clock = _Clock(epoch_offset=1_700_000_000)
    clock.now = 100
    hass = _Hass(lambda: 100, updated_at=1_700_000_000)
    window = MODULE.DoorEchoWindow()
    window.note_command(99)
    assert _run_guard(hass, clock, window) == "returned"
    assert hass.service_calls == []
    assert clock.sleeps == []


def test_real_epoch_fresh_echo_still_waits_full_travel() -> None:
    clock = _Clock(epoch_offset=1_700_000_000)
    clock.now = 100
    hass = _Hass(lambda: 100, updated_at=1_700_000_099)
    window = MODULE.DoorEchoWindow()
    window.note_command(99)
    assert _run_guard(hass, clock, window) == "returned"
    assert len(hass.service_calls) == 1
    assert clock.now >= 100 + MODULE.DEFAULT_GARAGE_DOOR_MIN_TRAVEL


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf"), 1_700_000_101, True, "bad"])
def test_invalid_or_future_epoch_change_time_is_unknown(monkeypatch, invalid) -> None:
    clock = _Clock(epoch_offset=1_700_000_000)
    clock.now = 100
    monkeypatch.setattr(MODULE, "time", _FakeTime(clock))
    hass = _Hass(lambda: 100, updated_at=invalid)
    assert MODULE._configured_cover_changed_at(hass, "cover.door") is None
