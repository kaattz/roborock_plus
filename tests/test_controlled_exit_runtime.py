"""What the controlled exit must actually do while the robot drives out.

Until this file the exit had no behavioural test at all -- only the option
schema was covered. That is how a total failure shipped: the arrival check read
``getattr(robot, 'position', None)`` on a dataclass that never had a ``position``
field, so the comparison was always ``None`` and the loop could only time out.

Measured 2026-11-08 10:46-10:48, on the first real run of this path:

    10:46:57  app_goto_target sent, state going_to_target
    10:47:00  outside_danger_zone on            <- robot is clear of the zone
    10:47:05  outside_danger_zone unknown       <- robot re-localising mid-trip
    10:47:59  outside_danger_zone on            <- located again, still clear
    10:48:08  task_active off, state idle       <- arrived, stopped
    ~10:48:57  120s timeout -> controlled_exit_failed
    then nothing: the door stayed open for hours, no alarm, phase stuck at
    controlled_exit, and the clean command was never sent.

So the tests below pin three separate obligations: the trip must end with the
door shut, a mid-trip localisation gap must not be read as "never arrived", and
a failure must leave the door in a state somebody can live with.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace as NS

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg.garage_door import GarageControlError
from _garage_pkg.garage_runtime import EVENT_GARAGE_ISSUE, GarageRuntime

DOCK = (25696.0, 28439.0)
WAYPOINT = (25696, 26500)
ZONE = NS(min_x=24997, max_x=26069, min_y=27123, max_y=28764)


class FakeStates:
    def __init__(self):
        self._states = {}

    def set(self, entity_id, state, **attributes):
        self._states[entity_id] = NS(state=state, attributes=dict(attributes), context=None)

    def get(self, entity_id):
        return self._states.get(entity_id)


class FakeServices:
    def __init__(self, on_call):
        self.calls = []
        self._on_call = on_call

    async def async_call(self, domain, service, data, blocking=False, context=None):
        self.calls.append((domain, service))
        self._on_call(service)


class FakeBus:
    def __init__(self):
        self.fired = []

    def async_fire(self, event, data):
        self.fired.append((event, data))

    def async_listen(self, event, callback):
        return lambda: None


class Robot:
    """A robot whose position and status the test moves explicitly."""

    def __init__(self):
        self.x, self.y = DOCK
        self.state = 'charging'
        self.trusted = True
        self.in_cleaning = 0
        self.in_returning = 0
        self.docked = True

    @property
    def outside(self):
        return not (ZONE.min_x <= self.x <= ZONE.max_x
                    and ZONE.min_y <= self.y <= ZONE.max_y)


class Coordinator:
    def __init__(self, robot):
        self.duid = 'robot'
        self.duid_slug = 'robot'
        self.last_update_success = True
        self.update_interval = dt.timedelta(seconds=1)
        self.garage_status_sample = None
        self.last_map_position_update = None
        self.garage_vacuum_entity_id = 'vacuum.robot'
        self.last_resume_command = None
        self.config_entry = NS(options={}, entry_id='entry')
        self.data = NS(clean_summary=None)
        self.robot = robot
        self.commands = []
        #: Distinct status polls this rig has published. The exit settles on
        #: *different* samples, so every observe() must look like a new poll.
        self.samples = 0
        self.now = 1000.0
        #: Whether each controller read also produces a new status poll, as the
        #: real coordinator does on its own clock.
        self.polling = True
        self.properties_api = NS(
            map_content=NS(map_data=NS(charger=NS(x=DOCK[0], y=DOCK[1]))),
            command=NS(send=self._send),
        )

    async def _send(self, name, params=None):
        self.commands.append((name, params))
        return {'ok': True}

    async def async_refresh(self):
        return None

    def resolve_vacuum_position(self):
        return NS(x=self.robot.x, y=self.robot.y, trusted=self.robot.trusted)

    def republish(self):
        """Publish a fresh poll of the robot's current state.

        The real coordinator polls on its own clock regardless of what any caller
        is doing, so a loop that waits for two *different* samples must be given
        new ones without the test scripting each. Only the sample identity moves;
        the physical facts stay whatever the test last set.
        """
        self.samples += 1
        stamp = dt.datetime.fromtimestamp(1000.0 + self.samples, dt.timezone.utc)
        self.garage_status_sample = {
            'sampled_at': stamp,
            'state': self.robot.state,
            'in_cleaning': self.robot.in_cleaning,
            'in_returning': self.robot.in_returning,
        }
        self.last_map_position_update = stamp
        self.now = 1000.0 + self.samples

    def observe(self, *, state=None, x=None, y=None, trusted=True):
        """Publish one status sample, as the coordinator does on every poll.

        Every call is by definition a successful read, so ``trusted`` defaults to
        True and only says otherwise when the test explicitly wants an untrusted
        sample. Leaving the previous flag in place would make one deliberate
        "re-localising" sample poison every later poll, which is not what a
        successful map read does.
        """
        if state is not None:
            self.robot.state = state
            self.robot.docked = state in ('charging', 'charging_complete')
            self.robot.in_cleaning = 1 if state in (
                'cleaning', 'segment_cleaning', 'zoned_cleaning', 'spot_cleaning') else 0
            self.robot.in_returning = 1 if state in (
                'returning_home', 'docking', 'going_to_wash_the_mop') else 0
        if x is not None:
            self.robot.x = x
        if y is not None:
            self.robot.y = y
        self.robot.trusted = trusted
        self.republish()


class Door:
    """Both contacts are the deployed polarity: 'off' at their own endpoint.

    Measured 2026-10-14: the physical close contact reads ``off`` when the door
    reaches the closed position (14:21:15.373), about five seconds before the
    cover reports ``closed``. Getting this backwards in a test rig makes a door
    that closed correctly look like a door that never moved.
    """

    def __init__(self, states):
        self.states = states
        self.open = False
        self.render()

    def render(self):
        self.states.set('cover.door', 'open' if self.open else 'closed',
                        current_position=100 if self.open else 0)
        self.states.set('binary_sensor.open', 'off' if self.open else 'on')
        self.states.set('binary_sensor.closed', 'off' if not self.open else 'on')

    def move(self, service):
        self.open = service == 'open_cover'
        self.render()


def build(*, timeout=30):
    states = FakeStates()
    robot = Robot()
    coordinator = Coordinator(robot)
    door = Door(states)
    services = FakeServices(door.move)
    binding = {
        'enabled': True,
        'cover_entity_id': 'cover.door',
        'task_door_mode': 'after_exit',
        'open_source': 'contact',
        'close_source': 'contact',
        'open_contact': 'binary_sensor.open',
        'close_contact': 'binary_sensor.closed',
        'open_contact_state': 'off',
        'close_contact_state': 'off',
        'contact_settle_seconds': 0,
        'handover_confirmed': True,
        'operation_timeout_seconds': 5,
        'controlled_exit_enabled': True,
        'controlled_exit_direction': 'north',
        'controlled_exit_distance_mm': 623,
        'controlled_exit_timeout_seconds': timeout,
    }
    hass = NS(states=states, services=services, bus=FakeBus(), data={})
    # A clock that advances on every read: the exit loop is deadline driven, so a
    # frozen clock would spin forever. Tying it to reads keeps the loop's own
    # timing logic intact while removing wall-clock time.
    ticks = {'n': 0}

    def clock():
        ticks['n'] += 1
        return float(ticks['n']) * 0.5

    runtime = GarageRuntime(
        hass, coordinator, binding, store=None,
        zone_provider=lambda: ZONE, clock=clock,
        # Wall clock follows the rig's published samples, as the real clock would
        # follow real polls; a frozen instant would make every sample look stale.
        utc=lambda: coordinator.now,
    )
    runtime.controller.clock = clock
    # Every controller read is also a coordinator poll: reproduce the real
    # situation, where the status trait keeps producing samples on its own.
    original_refresh = runtime.controller.refresh

    def refresh_and_poll():
        original_refresh()
        if coordinator.polling:
            coordinator.republish()
            original_refresh()

    runtime.controller.refresh = refresh_and_poll
    coordinator.observe(state='charging')
    runtime.controller.refresh()
    return runtime, coordinator, door, services


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    real_sleep = asyncio.sleep

    async def immediate(_delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, 'sleep', immediate)


def run(coro):
    return asyncio.run(coro)


def issues(runtime):
    return [data for event, data in runtime.hass.bus.fired if event == EVENT_GARAGE_ISSUE]


# --- the trip itself ---------------------------------------------------------

def test_the_door_is_shut_at_the_waypoint_and_only_then_is_the_clean_sent():
    runtime, coordinator, door, services = build()

    async def goto():
        coordinator.observe(state='going_to_target', x=WAYPOINT[0], y=28400.0)
        await asyncio.sleep(0)
        coordinator.observe(x=WAYPOINT[0], y=26500.0)
        coordinator.observe(state='idle')

    sent = []

    async def send():
        sent.append(door.open)
        return {'ok': True}

    runtime.async_control_command = _goto_then(goto)
    run(runtime.async_send('app_segment_clean', send))

    assert services.calls == [('cover', 'open_cover'), ('cover', 'close_cover')], (
        'the exit opens once and closes once')
    assert sent == [False], 'the clean runs with the door shut, behind a robot already outside'
    assert runtime.controller.phase == 'closed_outside'


def _goto_then(after):
    """Wrap the real controller command path so the test can script the trip."""

    async def command(name, params=None):
        assert name == 'app_goto_target'
        await after()
        return {'ok': True}

    return command


def test_a_relocalisation_gap_mid_trip_is_not_read_as_never_arriving():
    """The 2026-11-08 shape: outside, unknown for 54s, outside, then idle.

    A single-sample check would have to be lucky. The arrival condition has to
    survive the gap, which is why it is evaluated on the current sample and
    settles before acting rather than latching on the first true reading.
    """
    runtime, coordinator, door, services = build()

    async def goto():
        coordinator.observe(state='going_to_target', x=25696.0, y=28000.0)
        coordinator.observe(state='going_to_target', x=25696.0, y=27000.0)
        coordinator.observe(state='going_to_target', trusted=False)   # re-localising
        coordinator.observe(state='going_to_target', x=25696.0, y=26600.0)
        coordinator.observe(state='going_to_target', x=WAYPOINT[0], y=WAYPOINT[1])
        coordinator.observe(state='idle', x=WAYPOINT[0], y=WAYPOINT[1])

    sent = []

    async def send():
        sent.append(True)
        return {'ok': True}

    runtime.async_control_command = _goto_then(goto)
    run(runtime.async_send('app_segment_clean', send))

    assert ('cover', 'close_cover') in services.calls, 'the door must end up shut'
    assert sent == [True], 'the clean must still start'
    assert issues(runtime) == [], 'a trip that worked is not an incident'


def test_arrival_is_not_declared_while_the_robot_is_still_driving_out():
    """Confirmed outside is not the same as arrived: measured 10:47:00.

    The robot reported outside the zone three seconds after the goto was sent,
    while still driving. Closing there is the collision this whole sequence
    exists to avoid.
    """
    runtime, coordinator, door, services = build()

    async def goto():
        coordinator.observe(state='going_to_target', x=25696.0, y=27300.0)  # just out
        coordinator.observe(state='going_to_target', x=25696.0, y=27200.0)

    sent = []
    runtime.async_control_command = _goto_then(goto)

    async def send():
        sent.append(True)
        return {'ok': True}

    with pytest.raises(GarageControlError) as err:
        run(runtime.async_send('app_segment_clean', send))
    assert err.value.reason == 'controlled_exit_failed'
    assert ('cover', 'close_cover') not in services.calls, (
        'a robot still driving out must not have a door shut behind it')
    assert sent == []


# --- what a failed exit leaves behind ---------------------------------------

def test_a_failed_exit_shuts_the_door_when_the_robot_is_confirmed_outside():
    """Today's actual outcome: the trip worked, only the check was broken.

    A robot that is outside has no reason to be given an open door for hours, and
    the household gets told either way. This drives the failure handler directly,
    because a predicate that correctly reports "arrived" cannot also be a timeout
    -- the point here is what happens once an exit has been declared failed.
    """
    runtime, coordinator, door, services = build()

    async def scenario():
        await runtime.controller.open_door()
        runtime.controller.phase = 'controlled_exit'
        coordinator.observe(state='idle', x=WAYPOINT[0], y=WAYPOINT[1])
        runtime.controller.refresh()
        await runtime._abandon_controlled_exit(
            GarageControlError('controlled_exit_failed'))

    run(scenario())

    assert ('cover', 'close_cover') in services.calls, (
        'the robot is outside and stopped, so the door it came through is closed')
    assert runtime.controller.task_in_progress is False
    assert runtime.controller.last_error == 'controlled_exit_failed'
    assert [i['reason'] for i in issues(runtime)] == ['controlled_exit_failed']


def test_a_failed_exit_leaves_the_door_open_when_the_robot_is_not_confirmed_outside():
    runtime, coordinator, door, services = build(timeout=8)

    async def goto():
        # Stuck inside the zone: the exact case where closing would clamp it.
        coordinator.observe(state='going_to_target', x=25696.0, y=28000.0)

    runtime.async_control_command = _goto_then(goto)
    sent = []

    async def send():
        sent.append(True)
        return {'ok': True}

    with pytest.raises(GarageControlError):
        run(runtime.async_send('app_segment_clean', send))

    assert sent == []
    assert ('cover', 'close_cover') not in services.calls
    assert runtime.controller.task_in_progress is False
    assert runtime.controller.last_error == 'controlled_exit_failed'
    assert issues(runtime) and issues(runtime)[0]['reason'] == 'controlled_exit_failed'


def test_the_exit_failure_is_reported_with_its_own_reason_not_a_door_fault():
    """`garage_problem` must stop reading "off" while a sequence is stuck.

    The phase stays honest about the door (open is open); the fault lives in
    ``last_error``, which is what ``garage_problem`` and its ``reason`` attribute
    are built from. Calling this ``blocked`` would misreport a readable door as
    an unreadable one, and send the household looking for a sensor fault.
    """
    runtime, coordinator, door, services = build(timeout=8)
    coordinator.observe(state='going_to_target', x=25696.0, y=28000.0)

    async def goto():
        coordinator.observe(state='going_to_target', x=25696.0, y=28000.0)

    runtime.async_control_command = _goto_then(goto)

    async def send():
        raise AssertionError('must not run')

    with pytest.raises(GarageControlError):
        run(runtime.async_send('app_segment_clean', send))

    assert runtime.controller.last_error == 'controlled_exit_failed'
    assert runtime.controller.phase == 'outside', 'the door is open and readable'
    assert runtime.controller.door_blocked_reason() is None


# --- the observation must carry what the check reads -------------------------

def test_a_robot_idle_but_still_inside_is_never_an_arrival():
    """`trip_finished` alone says "stopped", not "got out".

    A goto that is cancelled, or one that fails and leaves the robot parked mid
    cabinet, also ends with ``idle``. Treating that as arrival would shut the door
    on a robot standing in the room it is trying to leave.
    """
    runtime, coordinator, door, services = build()
    # Outside the waypoint tolerance, and still inside the danger zone.
    coordinator.observe(state='idle', x=25696.0, y=28000.0)
    runtime.controller.refresh()

    assert runtime.controller.trip_finished() is True
    assert runtime.controller.robot_outside() is False
    assert runtime.controller.controlled_exit_arrived(WAYPOINT) is False


def test_a_coordinate_hit_alone_does_not_release_the_door():
    """A coordinate near the waypoint is not permission to shut the door.

    The waypoint is only 623mm beyond the danger zone's lower edge, so a 400mm
    tolerance reaches back to within 223mm of it. Being "at the waypoint" and
    being "clear of the door" are different questions, and the second one is the
    one that decides whether a door may move.
    """
    runtime, coordinator, door, services = build()
    # On the coordinate, but the safety reading says it is not outside.
    coordinator.observe(state='going_to_target', x=WAYPOINT[0], y=WAYPOINT[1])
    runtime.controller.refresh()
    runtime.controller.robot = runtime.controller.robot._replace(outside_danger_zone=False)

    assert runtime.controller.contained_position() is not None
    assert runtime.controller.controlled_exit_arrived(WAYPOINT) is False


def test_a_single_sample_of_trip_end_is_not_enough_to_close_the_door():
    """The settle must count distinct polls, not repeated reads of one sample.

    ``refresh()`` returns the last cached status sample, so a loop that treats
    each wake-up as fresh confirmation would act on one reading. Here the robot
    reports its trip over exactly once and then never polled again: two different
    samples are required, so the exit must refuse and time out.
    """
    runtime, coordinator, door, services = build()
    coordinator.polling = False    # freeze polling: only the scripted sample exists

    async def goto():
        coordinator.observe(state='idle', x=WAYPOINT[0], y=WAYPOINT[1])

    runtime.async_control_command = _goto_then(goto)
    with pytest.raises(GarageControlError) as err:
        run(runtime._run_controlled_exit())
    assert err.value.reason == 'controlled_exit_failed'
    assert runtime.controller.phase != 'closed_outside'

    # The same reading, confirmed by a second poll, is enough.
    runtime2, coordinator2, door2, services2 = build()

    async def goto2():
        coordinator2.observe(state='idle', x=WAYPOINT[0], y=WAYPOINT[1])

    runtime2.async_control_command = _goto_then(goto2)
    run(runtime2._run_controlled_exit())
    assert ('cover', 'close_cover') in services2.calls


def test_the_arrival_offset_is_reported_when_the_robot_stops_short():
    """The trip ending closes the exit; stopping short must still be visible.

    Silently accepting a waypoint the robot never reached would let the household
    believe the configured distance was honoured.
    """
    runtime, coordinator, door, services = build()

    async def goto():
        coordinator.observe(state='going_to_target', x=WAYPOINT[0], y=27200.0)
        # Trip ends just outside the zone edge, well short of the 26500 waypoint.
        coordinator.observe(state='idle', x=WAYPOINT[0], y=27050.0)

    runtime.async_control_command = _goto_then(goto)
    sent = []

    async def send():
        sent.append(True)
        return {'ok': True}

    run(runtime.async_send('app_segment_clean', send))

    assert ('cover', 'close_cover') in services.calls, (
        'the trip ended and the robot is outside, so the door is safe to close')
    assert sent == [True]
    assert runtime.exit_arrival_offset_mm == pytest.approx(550.0), (
        'the difference between the waypoint and where it stopped must be recorded')


def test_a_short_configured_distance_is_actually_waited_for():
    """The 100mm setting: the exit must not treat the zone edge as the target.

    This drives the controller the way the household configured it -- 100mm past
    the edge -- and requires the exit to wait for the trip to finish rather than
    accepting the first position that happens to be outside.
    """
    runtime, coordinator, door, services = build()
    runtime.binding['controlled_exit_distance_mm'] = 100

    polls = {'n': 0}

    async def goto():
        # Clearing the edge is not arriving when the waypoint is only 100mm out.
        coordinator.observe(state='going_to_target', x=WAYPOINT[0], y=27120.0)

    runtime.async_control_command = _goto_then(goto)
    original_refresh = runtime.controller.refresh

    def refresh_then_finish():
        original_refresh()
        polls['n'] += 1
        if polls['n'] >= 3:
            # The robot finishes the trip at the requested 100mm point.
            coordinator.observe(state='idle', x=WAYPOINT[0], y=27023.0)

    runtime.controller.refresh = refresh_then_finish

    sent = []

    async def send():
        sent.append(True)
        return {'ok': True}

    run(runtime.async_send('app_segment_clean', send))

    assert ('cover', 'close_cover') in services.calls
    assert sent == [True]
    assert runtime.exit_arrival_offset_mm == pytest.approx(0.0)


def test_a_sideways_legacy_waypoint_warns_about_the_dropped_offset(caplog):
    """The new model cannot express a lateral offset; say so rather than hide it.

    A pre-0.12.0 binding could place the waypoint anywhere. Converting it keeps
    only the along-axis component, so a waypoint that was mostly sideways ends up
    describing a different path. Warning is the difference between "the upgrade
    moved my robot and nobody said" and "the upgrade moved my robot and the log
    said why".
    """
    runtime, coordinator, door, services = build()
    for key in ('controlled_exit_distance_mm', 'controlled_exit_direction'):
        runtime.binding.pop(key, None)
    runtime.binding['controlled_exit_direction'] = 'north'
    runtime.binding['controlled_exit_x'] = WAYPOINT[0] + 1500
    runtime.binding['controlled_exit_y'] = WAYPOINT[1]

    with caplog.at_level('WARNING'):
        runtime._controlled_exit_settings()
    assert any('lateral' in record.message for record in caplog.records), (
        'dropping 1500mm of sideways offset must be reported')


def test_the_robot_observation_carries_a_usable_position():
    """The regression that caused the incident, pinned at its narrowest.

    The check reads ``robot.x`` / ``robot.y``. If those fields are missing the
    check silently degrades to "never arrived", which is why a missing field must
    fail a test rather than a timeout six weeks later.
    """
    runtime, coordinator, door, services = build()
    coordinator.observe(state='going_to_target', x=25696.0, y=27000.0)
    runtime.controller.refresh()
    observation = runtime.controller.robot

    assert observation.x == pytest.approx(25696.0)
    assert observation.y == pytest.approx(27000.0), (
        'the coordinate the arrival check needs must be on the observation')
    assert runtime.controller.controlled_exit_arrived(WAYPOINT) is False, (
        'outside is not enough: 500mm short of the waypoint and still driving')

    coordinator.observe(state='idle', x=25696.0, y=26501.0)
    runtime.controller.refresh()
    assert runtime.controller.controlled_exit_arrived(WAYPOINT) is True

    coordinator.observe(trusted=False)
    runtime.controller.refresh()
    assert runtime.controller.contained_position() is None, (
        'an untrusted sample must not be offered as a coordinate')
