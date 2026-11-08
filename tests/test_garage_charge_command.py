"""Pressing "return to base" during a clean must not aim the robot at a shut door.

The household presses it from Home Assistant. `vacuum.return_to_base` sends
`app_charge`, which the garage runtime used to forward untouched. That is correct
for a docked or idle robot, and wrong in the one case that matters: a clean is
running, the robot is beyond the door, and the departure rule has already shut it.
Nothing opened it again, so the robot drove at a closed door.

The chosen behaviour (2026-11-08) is the careful one:

    pause -> open, confirmed -> send app_charge

The pause is not about the door. A robot already on its way home may be metres
away and would turn back; stopping it first means nothing moves while the door
does. Once the way is clear it is released.

Everything else keeps the old behaviour, because a command sent late is a
different bug from a command sent into a shut door.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg.garage_door import GarageControlError
from _garage_pkg.garage_runtime import GarageRuntime

COVER = "cover.garage"


class Rig:
    """A robot and a door whose every change is recorded in order."""

    def __init__(self, *, state="cleaning", docked=False, outside=True,
                 task_active=True, opened=False):
        self.events = []
        self.state = state
        self.docked = docked
        self.outside = outside
        self.task_active = task_active
        self.opened = opened
        self.door_broken = False
        self.samples = 0
        self.now = 1000.0

    # -- home assistant surface ------------------------------------------------
    def states(self):
        rig = self

        class States:
            def get(self, entity_id):
                if entity_id == COVER:
                    if rig.door_broken:
                        return NS(state="unavailable", attributes={})
                    return NS(state="open" if rig.opened else "closed", attributes={
                        "current_position": 100 if rig.opened else 0,
                    })
                if entity_id == "binary_sensor.open":
                    return NS(state="off" if rig.opened else "on", attributes={})
                if entity_id == "binary_sensor.closed":
                    return NS(state="on" if rig.opened else "off", attributes={})
                return None

        return States()

    async def async_call(self, domain, service, data, blocking=False, context=None):
        """Match Home Assistant's service-call signature, including `blocking`."""
        self.events.append(service)
        if service == "open_cover":
            self.opened = True
        elif service == "close_cover":
            self.opened = False

    def bus(self):
        return NS(async_fire=lambda *a, **k: None,
                  async_listen=lambda *a, **k: (lambda: None))

    # -- the robot -------------------------------------------------------------
    def observe(self):
        self.samples += 1
        self.now += 1.0
        stamp = NS()
        import datetime as dt
        stamp = dt.datetime.fromtimestamp(self.now, dt.timezone.utc)
        self.sample = {
            "sampled_at": stamp, "state": self.state,
            "in_cleaning": 1 if self.state in ("cleaning", "segment_cleaning") else 0,
            "in_returning": 1 if self.state in ("returning_home", "docking") else 0,
        }
        self.map_stamp = stamp

    async def send(self, name, params=None):
        self.events.append(f"robot:{name}")
        return {"ok": True}


def build(*, state="cleaning", docked=False, outside=True, task_active=True,
          opened=False):
    rig = Rig(state=state, docked=docked, outside=outside,
              task_active=task_active, opened=opened)
    rig.observe()

    class Coordinator:
        def __init__(self):
            self.duid = "robot"
            self.duid_slug = "robot"
            self.last_update_success = True
            self.update_interval = __import__("datetime").timedelta(seconds=1)
            self.garage_status_sample = None
            self.last_map_position_update = None
            self.config_entry = NS(options={}, entry_id="entry")
            self.data = NS(clean_summary=None)
            self.robot = rig
            self.samples = 0
            self.now = 1000.0
            self.properties_api = NS(
                map_content=NS(map_data=NS(charger=NS(x=25696.0, y=28439.0))),
                command=NS(send=rig.send),
            )

        async def async_refresh(self):
            return None

        def resolve_vacuum_position(self):
            # Coordinates must agree with the `outside` flag: the observation layer
            # derives `outside_danger_zone` from the position, so a fixed point
            # here would make the flag meaningless.
            return NS(x=25696.0,
                      y=26500.0 if rig.outside else 28400.0,
                      trusted=True)

    coordinator = Coordinator()
    coordinator.garage_status_sample = rig.sample
    coordinator.last_map_position_update = rig.map_stamp

    binding = {
        "enabled": True, "cover_entity_id": COVER, "task_door_mode": "after_exit",
        "open_source": "contact", "close_source": "contact",
        "open_contact": "binary_sensor.open", "close_contact": "binary_sensor.closed",
        "open_contact_state": "off", "close_contact_state": "off",
        "position_settle_seconds": 0, "contact_settle_seconds": 0,
        "handover_confirmed": True, "operation_timeout_seconds": 5,
        "dock_settle_seconds": 1,
    }
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        return float(ticks["n"]) * 0.5

    hass = NS(states=rig.states(), services=rig, bus=rig.bus(), data={})
    runtime = GarageRuntime(
        hass, coordinator, binding, store=None,
        zone_provider=lambda: NS(min_x=24997, max_x=26069, min_y=27123, max_y=28764),
        clock=clock,
        # Follow the sample clock: the freshness rule rejects a status sample
        # older than twice the poll interval, so a frozen "now" would make every
        # observation stale and `outside_danger_zone` would silently read None.
        utc=lambda: rig.now,
    )
    runtime.controller.clock = clock
    runtime.controller.refresh()
    return runtime, rig


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    real = asyncio.sleep

    async def immediate(_d, *a, **k):
        await real(0)

    monkeypatch.setattr(asyncio, "sleep", immediate)


def run(coro):
    return asyncio.run(coro)


# --- the dangerous case ------------------------------------------------------

def test_a_working_robot_outside_is_paused_then_let_in():
    """THE SHUT-DOOR CASE.

    Clean running, robot beyond the door, door shut. The command must not reach
    the robot until the door is confirmed open, and the robot must be stopped
    while that happens.
    """
    runtime, rig = build(state="cleaning", outside=True, opened=False)

    run(runtime.async_send("app_charge", lambda: rig.send("app_charge")))

    assert "robot:app_pause" in rig.events, (
        "the robot must be stopped before anything else moves")
    assert "open_cover" in rig.events
    pause_at = next(i for i, e in enumerate(rig.events) if "pause" in e)
    open_at = rig.events.index("open_cover")
    charge_at = rig.events.index("robot:app_charge")
    assert pause_at < open_at < charge_at, (
        f"order must be pause -> open -> charge, got {rig.events}")


def test_the_command_is_never_sent_while_the_door_cannot_open():
    """Refusing beats driving the robot into a door that would not budge."""
    runtime, rig = build(state="cleaning", outside=True, opened=False)
    rig.door_broken = True

    with pytest.raises(GarageControlError):
        run(runtime.async_send("app_charge", lambda: rig.send("app_charge")))
    assert "robot:app_charge" not in rig.events, (
        "a door that will not open must stop the command, not be ignored")


# --- the cases that must stay untouched --------------------------------------

def test_a_docked_robot_is_sent_home_without_touching_the_door():
    """Nothing to open for a robot already inside; opening would be an unasked move."""
    runtime, rig = build(state="charging", docked=True, outside=False,
                         task_active=True, opened=False)

    run(runtime.async_send("app_charge", lambda: rig.send("app_charge")))

    assert rig.events == ["robot:app_charge"]


def test_an_idle_robot_is_sent_home_without_touching_the_door():
    runtime, rig = build(state="idle", docked=False, outside=True,
                         task_active=False, opened=False)

    run(runtime.async_send("app_charge", lambda: rig.send("app_charge")))

    assert rig.events == ["robot:app_charge"]


def test_a_robot_already_inside_the_zone_needs_no_passage():
    """Working but not outside: the door is not between it and the dock."""
    runtime, rig = build(state="cleaning", outside=False, opened=False)

    run(runtime.async_send("app_charge", lambda: rig.send("app_charge")))

    assert "open_cover" not in rig.events
    assert rig.events == ["robot:app_charge"]


def test_a_failed_pause_still_opens_the_door():
    """Opening is the safe part even if stopping did not take.

    An open door cannot be driven into; a shut one can. So a robot that would not
    stop is still let through.
    """
    runtime, rig = build(state="cleaning", outside=True, opened=False)

    async def no_pause_effect(name, params=None):
        rig.events.append(f"robot:{name}")   # pause "works" but state never changes
        return {"ok": True}

    runtime.coordinator.properties_api.command = NS(send=no_pause_effect)

    run(runtime.async_send("app_charge", lambda: rig.send("app_charge")))

    assert "open_cover" in rig.events, (
        "the door must open even when the robot would not stop")
    assert "robot:app_charge" in rig.events
