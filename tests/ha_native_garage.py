"""Native Home Assistant checks for the garage binding.

Only what genuinely needs a real HomeAssistant instance lives here: module
imports, the service registration, and end-to-end door sequences. The door rules
themselves are pure and covered by ``test_garage_door_rules.py``; the runtime
behaviour by ``test_garage_runtime_behaviour.py``.

The old file also replayed session-based scenarios (goto authority, external task
revocation, staged receipts). Those mechanisms are gone, and replaying them here
would only assert that they stay gone -- which the absence of the modules already
says.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS

import pytest
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section

import voluptuous as vol

PACKAGE = "custom_components.roborock_plus"


async def _ok():
    return None


def _binding():
    return {
        'enabled': True, 'cover_entity_id': 'cover.memory_garage',
        'task_door_mode': 'after_exit', 'open_source': 'contact',
        'close_source': 'contact',
        'open_contact': 'binary_sensor.memory_open',
        'close_contact': 'binary_sensor.memory_closed',
        'open_contact_state': 'on', 'close_contact_state': 'on',
        'contact_settle_seconds': 0, 'handover_confirmed': True,
        'operation_timeout_seconds': 5,
    }


def _coordinator(moment):
    summary = NS(records=[1], clean_count=1)
    status = NS(state=NS(name="charging"), in_cleaning=0, in_returning=0)
    return NS(
        duid="memory-robot", duid_slug="memory_robot",
        config_entry=NS(options={}, entry_id="entry"),
        last_update_success=True, update_interval=dt.timedelta(seconds=1),
        data=NS(status=status, clean_summary=summary),
        properties_api=NS(
            map_content=NS(map_data=NS(charger=NS(x=0.0, y=0.0))),
            command=NS(send=lambda name, params=None: _ok()),
        ),
        resolve_vacuum_position=lambda: NS(x=0.0, y=0.0, trusted=True),
        garage_status_sample={
            'sampled_at': dt.datetime.fromtimestamp(moment['utc'], dt.timezone.utc),
            'state': 'charging', 'in_cleaning': 0, 'in_returning': 0,
        },
        last_map_position_update=dt.datetime.fromtimestamp(moment['utc'], dt.timezone.utc),
        garage_vacuum_entity_id="vacuum.memory_robot",
        last_resume_command=None,
        async_refresh=_ok,
        async_add_listener=lambda _: (lambda: None),
    )


class MemoryEntries:
    """No account setup or reload; only the native flow's entry lookup boundary."""

    def __init__(self, entry):
        self.entry = entry

    @callback
    def async_entries(self, domain):
        return [self.entry] if domain == "roborock_plus" else []

    @callback
    def async_update_entry(self, entry, *, options):
        entry.options = deepcopy(options)
        return True


def options_schema_input(schema, flat_values):
    """Validate a payload grouped by the actual HA section schema, if present."""

    def group(current_schema):
        result = {}
        for marker, validator in current_schema.schema.items():
            key = getattr(marker, "schema", marker)
            if isinstance(validator, section):
                # Real frontend submits each visible section. With no root
                # default, the section's own schema must fill its children.
                result[key] = group(validator.schema)
            elif key in flat_values:
                result[key] = flat_values[key]
            elif "suggested_value" in (getattr(marker, "description", None) or {}):
                # Match the frontend's initial field population. Suggestions are
                # not backend defaults and raw manager payloads may omit or clear
                # them afterwards to exercise actual validation.
                result[key] = marker.description["suggested_value"]
        return result

    return schema(group(schema))


def test_native_imports_all_changed_production_modules():
    for name in (
        "config_flow", "coordinator", "garage_config", "door_evidence",
        "garage_commands", "garage_door", "garage_runtime", "garage_entities",
        "binary_sensor", "sensor", "vacuum", "button", "services",
    ):
        module = importlib.import_module(f"{PACKAGE}.{name}")
        assert module.__file__.startswith(str(Path(__file__).resolve().parents[1]))


def test_native_service_registration_and_forbidden_force_argument():
    async def exercise():
        services = importlib.import_module(f"{PACKAGE}.services")
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            services.async_setup_services(hass)
            registered = hass.services.async_services()["roborock_plus"]
            for name in ("request_garage_close", "request_garage_open", "reset_garage_control"):
                assert hass.services.has_service("roborock_plus", name)
                schema = registered[name].schema
                schema({"entity_id": "vacuum.memory_robot"})
                with pytest.raises(vol.Invalid):
                    schema({"entity_id": "vacuum.memory_robot", "force": True})
            await hass.async_block_till_done()

    asyncio.run(exercise())


def test_native_controller_opens_then_closes_around_a_robot_trip():
    """One full sequence against real Home Assistant state and services.

    This is the shape the household cares about: the door opens before the robot
    is released, and shuts once it is confirmed outside. Everything else in the
    design is a variation on it.
    """
    async def exercise():
        runtime_module = importlib.import_module(f"{PACKAGE}.garage_runtime")
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            moment = {"now": 0.0, "utc": 1000.0}
            door_commands = []

            @callback
            def state_door(opened, context=None):
                hass.states.async_set("cover.memory_garage", "open" if opened else "closed",
                                      {"current_position": 100 if opened else 0},
                                      context=context)
                hass.states.async_set("binary_sensor.memory_open", "on" if opened else "off",
                                      context=context)
                hass.states.async_set("binary_sensor.memory_closed", "off" if opened else "on",
                                      context=context)

            state_door(False)

            async def cover_service(call):
                door_commands.append(call.service)
                state_door(call.service == "open_cover", call.context)

            for service in ("open_cover", "close_cover", "stop_cover"):
                hass.services.async_register("cover", service, cover_service)

            coordinator = _coordinator(moment)

            # A real robot reports itself paused when it accepts app_pause, and
            # moving again after app_resume. Without that the controller correctly
            # refuses to move the door, because it cannot confirm the pause.
            async def robot_command(name, params=None):
                state = {'app_pause': 'paused', 'app_resume': 'cleaning'}.get(name)
                if state is not None and coordinator.garage_status_sample is not None:
                    coordinator.garage_status_sample = {
                        **coordinator.garage_status_sample, 'state': state,
                    }
                return {'ok': True}

            coordinator.properties_api = NS(
                map_content=NS(map_data=NS(charger=NS(x=0.0, y=0.0))),
                command=NS(send=robot_command),
            )
            runtime = runtime_module.GarageRuntime(
                hass, coordinator, _binding(),
                zone_provider=lambda: NS(min_x=-10000, max_x=10000,
                                         min_y=-10000, max_y=10000),
                clock=lambda: moment['now'], utc=lambda: moment['utc'],
            )
            coordinator.garage_runtime = runtime

            try:
                # 1. A clean opens the door and confirms it before releasing.
                await runtime.async_send('app_segment_clean', _ok)
                assert door_commands == ['open_cover']
                assert runtime.controller.open_confirmed() is True

                # 2. The robot drives out; the door shuts once it is outside.
                coordinator.data.status.state = NS(name='cleaning')
                coordinator.data.status.in_cleaning = 1
                coordinator.garage_status_sample = {
                    'sampled_at': dt.datetime.fromtimestamp(moment['utc'], dt.timezone.utc),
                    'state': 'cleaning', 'in_cleaning': 1, 'in_returning': 0,
                }
                coordinator.resolve_vacuum_position = lambda: NS(x=0.0, y=50000.0, trusted=True)
                await runtime.async_tick()
                assert door_commands == ['open_cover', 'close_cover']
                assert runtime.controller.phase == 'closed_outside'

                # 3. It comes home; the door reopens for the return, then closes
                #    for good once the robot is docked and idle.
                coordinator.data.status.state = NS(name='charging')
                coordinator.data.status.in_cleaning = 0
                coordinator.garage_status_sample = {
                    'sampled_at': dt.datetime.fromtimestamp(moment['utc'], dt.timezone.utc),
                    'state': 'charging', 'in_cleaning': 0, 'in_returning': 0,
                }
                coordinator.resolve_vacuum_position = lambda: NS(x=0.0, y=0.0, trusted=True)
                await runtime.async_request_open()
                assert door_commands[-1] == 'open_cover'
                await runtime.async_tick()
                assert door_commands[-1] == 'close_cover'
                assert runtime.controller.phase == 'idle'
            finally:
                runtime.shutdown()
                await hass.async_block_till_done()

    asyncio.run(exercise())


def test_native_nothing_latches_after_a_failed_attempt():
    """A refused clean leaves no state that needs clearing.

    The whole reason for the rewrite: the previous design raised a hold that the
    reset service itself refused to clear.
    """
    async def exercise():
        runtime_module = importlib.import_module(f"{PACKAGE}.garage_runtime")
        door_module = importlib.import_module(f"{PACKAGE}.garage_door")
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            hass.states.async_set("cover.memory_garage", "unavailable")
            moment = {"now": 0.0, "utc": 1000.0}
            coordinator = _coordinator(moment)
            runtime = runtime_module.GarageRuntime(
                hass, coordinator, _binding(),
                zone_provider=lambda: NS(min_x=-10000, max_x=10000,
                                         min_y=-10000, max_y=10000),
            )
            coordinator.garage_runtime = runtime
            try:
                for _ in range(3):
                    with pytest.raises(door_module.GarageControlError):
                        await runtime.async_send('app_segment_clean', _ok)
                assert runtime.controller.phase == 'blocked'

                # The evidence comes back: no reset, no service call, nothing.
                hass.states.async_set("cover.memory_garage", "closed",
                                      {"current_position": 0})
                hass.states.async_set("binary_sensor.memory_open", "off")
                hass.states.async_set("binary_sensor.memory_closed", "on")
                opened = []

                async def cover_service(call):
                    opened.append(call.service)
                    # A real cover reports its new endpoint; without this the
                    # controller would correctly refuse to claim it opened.
                    hass.states.async_set("cover.memory_garage", "open",
                                          {"current_position": 100}, context=call.context)
                    hass.states.async_set("binary_sensor.memory_open", "on")
                    hass.states.async_set("binary_sensor.memory_closed", "off")

                hass.services.async_register("cover", "open_cover", cover_service)
                await runtime.async_request_open()
                assert opened == ['open_cover']
                assert runtime.controller.last_error is None
            finally:
                runtime.shutdown()
                await hass.async_block_till_done()

    asyncio.run(exercise())
