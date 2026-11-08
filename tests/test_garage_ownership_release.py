"""A reloaded integration must be able to take its own door back.

History: `GarageRuntime.shutdown()` cancelled tasks and dropped listeners, but left
itself in the process-wide `owners` map under the cover entity id. A reload then
found the door "already owned" by a runtime that no longer existed, raised
`duplicate_binding`, and the `except` around setup turned that into
`coordinator.garage_setup_error`. The runtime was never attached, so every clean
command hit `session_recovery_required` -- the robot beeped and did nothing.

Observed 2026-11-08 13:50, immediately after a reload:
    binary_sensor.ke_ting_sao_di_ji_garage_control_problem  on
    reason = duplicate_binding
    phase  = blocked

The leak is invisible until something reloads, which is why it survived two
rewrites: no test ever built a runtime, shut it down, and asked for the door again.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg import garage_runtime
from _garage_pkg.garage_runtime import GarageRuntime


COVER = "cover.garage"


def _states():
    class States:
        def __init__(self):
            self._states = {}

        def set(self, entity_id, state, **attributes):
            self._states[entity_id] = NS(state=state, attributes=dict(attributes), context=None)

        def get(self, entity_id):
            return self._states.get(entity_id)

    states = States()
    states.set(COVER, "closed", current_position=0)
    states.set("binary_sensor.open", "on")
    states.set("binary_sensor.closed", "off")
    return states


def _runtime(hass, duid="robot-a"):
    """A runtime with inert dependencies; only ownership bookkeeping is tested."""
    binding = {
        "enabled": True,
        "cover_entity_id": COVER,
        "task_door_mode": "after_exit",
        "open_source": "contact",
        "close_source": "contact",
        "open_contact": "binary_sensor.open",
        "close_contact": "binary_sensor.closed",
        "open_contact_state": "on",
        "close_contact_state": "off",
        "contact_settle_seconds": 0,
        "handover_confirmed": True,
        "operation_timeout_seconds": 5,
        "dock_settle_seconds": 1,
    }
    coordinator = NS(
        duid=duid, duid_slug=duid,
        config_entry=NS(options={}, entry_id="entry"),
        data=NS(clean_summary=None),
        properties_api=NS(
            map_content=NS(map_data=NS(charger=NS(x=0.0, y=0.0))),
            command=NS(send=_noop_send),
        ),
    )
    return GarageRuntime(
        hass, coordinator, binding, store=None,
        zone_provider=lambda: None, clock=lambda: 0.0, utc=lambda: 0.0,
    )


async def _noop_send(name, params=None):
    return {"ok": True}


def _hass():
    return NS(
        states=_states(),
        services=NS(async_call=_noop_call),
        bus=NS(async_fire=lambda *a, **k: None, async_listen=lambda *a, **k: (lambda: None)),
        data={},
    )


async def _noop_call(*args, **kwargs):
    return None


def test_shutdown_releases_the_door_for_the_next_runtime():
    """The whole defect, at its smallest.

    Without the release the second registration sees a stale owner and the door
    can never come back after any reload.
    """
    hass = _hass()
    owners = hass.data.setdefault(garage_runtime.REGISTRY_KEY, {})

    first = _runtime(hass)
    owners[COVER] = first
    first.shutdown()

    assert COVER not in owners, (
        "a shut-down runtime must not keep owning the door; otherwise the next "
        "setup raises duplicate_binding and every clean is refused")

    second = _runtime(hass, duid="robot-a")
    assert COVER not in owners
    owners[COVER] = second
    assert owners[COVER] is second


def test_shutdown_leaves_another_runtime_alone():
    """Only release the entry that is actually mine.

    A second entry managing a different door must be untouched, and a map entry
    that already points at somebody else must not be deleted by my teardown.
    """
    hass = _hass()
    owners = hass.data.setdefault(garage_runtime.REGISTRY_KEY, {})

    mine = _runtime(hass)
    other = object()
    owners[COVER] = mine
    owners["cover.other"] = other

    mine.shutdown()

    assert "cover.other" in owners and owners["cover.other"] is other
    assert COVER not in owners


def test_shutdown_is_idempotent():
    """Unload can run twice; the second pass must not raise or remove a new owner."""
    hass = _hass()
    owners = hass.data.setdefault(garage_runtime.REGISTRY_KEY, {})
    mine = _runtime(hass)
    owners[COVER] = mine
    mine.shutdown()

    successor = object()
    owners[COVER] = successor
    mine.shutdown()          # late second call, after a new owner appeared

    assert owners.get(COVER) is successor, (
        "a stale runtime must not evict the runtime that replaced it")


def test_setup_after_shutdown_no_longer_reports_duplicate_binding():
    """The user-visible symptom, expressed as the ownership check it fails on.

    The real symptom was `reason = duplicate_binding` on
    `garage_control_problem`, produced by exactly this branch:

        if binding['cover_entity_id'] in owners:
            raise GarageControlError('duplicate_binding')

    `async_setup_garages` itself needs a live HomeAssistant object, so the branch
    is exercised directly rather than through the whole setup function; the state
    that decides it is the map this file owns.
    """
    hass = _hass()
    owners = hass.data.setdefault(garage_runtime.REGISTRY_KEY, {})

    first = _runtime(hass)
    owners[COVER] = first
    first.shutdown()

    # This is the predicate that raised, and what it must now answer.
    assert COVER not in owners, "the reload would raise duplicate_binding again"
    assert first.binding["cover_entity_id"] == COVER
