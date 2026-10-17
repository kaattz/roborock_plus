"""Actual HA entity-service dispatch into V1 methods, with memory transport."""

import asyncio
from datetime import timedelta
import importlib
import json
import logging
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS

import pytest
from homeassistant.components.vacuum import StateVacuumEntity, VacuumActivity
from homeassistant.core import Context, HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util.yaml import parse_yaml

from ha_native_garage_feedback_recovery import NativeFeedbackRecovery
from ha_native_garage_staged_cleaning import STAGE
from ha_native_staged_cleaning_script import make_script, SENSOR


VACUUM = 'vacuum.memory_robot'


async def setup_memory_entity(hass, rig, protocol='RoborockVacuum', started=None):
    # Match the complete registry fixture already exercised in ha_native_garage.
    hass.data[dr.DATA_REGISTRY] = dr.DeviceRegistry(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    module = importlib.import_module('custom_components.roborock_plus.vacuum')
    sent = []
    refreshes = []

    async def send(command, *, params=None):
        sent.append((command, params))
        if command == STAGE['command']:
            await rig.start_robot()
            if started is not None:
                started.set()
        return {'accepted': True}

    async def refresh():
        refreshes.append(True)

    rig.coordinator.async_refresh = refresh

    class MemoryV1(getattr(module, protocol)):
        """Replace only device/entity presentation; keep the V1 service and send."""
        _attr_should_poll = False
        _attr_name = 'Memory stage robot'
        _attr_has_entity_name = False
        _attr_device_info = None
        _attr_unique_id = None

        def __init__(self):
            StateVacuumEntity.__init__(self)
            self.entity_id = VACUUM
            self.coordinator = rig.coordinator
            self._api = NS(send=send)

        @property
        def available(self):
            return True

        @property
        def activity(self):
            return VacuumActivity.DOCKED

        @property
        def fan_speed(self):
            return None

        @property
        def fan_speed_list(self):
            return []

        async def async_added_to_hass(self):
            pass

    platform = EntityPlatform(
        hass=hass, logger=logging.getLogger(__name__), domain='vacuum',
        platform_name='roborock_plus', platform=None,
        scan_interval=timedelta(seconds=60), entity_namespace=None,
    )
    await platform.async_add_entities([MemoryV1()])
    importlib.import_module('custom_components.roborock_plus.services').async_setup_services(hass)
    return platform, sent, refreshes


def test_native_registered_entity_service_returns_wrapped_receipt_through_actual_v1_send():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, 'hold_open')
            platform = None
            try:
                await rig.initialize()
                platform, sent, refreshes = await setup_memory_entity(hass, rig)
                registered = hass.services.async_services()['roborock_plus']['start_garage_cleaning_stage']
                assert registered.supports_response is SupportsResponse.ONLY
                response = await hass.services.async_call(
                    'roborock_plus', 'start_garage_cleaning_stage',
                    {'entity_id': VACUUM, 'stage': STAGE}, blocking=True, return_response=True,
                )
                assert set(response) == {VACUUM}
                assert response[VACUUM] == rig.runtime._stage.receipt()
                assert response[VACUUM]['session_id'] == rig.runtime.session.session_id
                assert sent == [(s['command'], s['params']) for s in STAGE['settings']] + [(STAGE['command'], STAGE['params'])]
                assert len(refreshes) == len(sent)
                assert rig.commands == ['open_cover', 'robot_start']
                assert rig.events[1][1] - rig.events[0][1] == 45
            finally:
                if platform is not None:
                    await platform.async_reset()
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


def test_native_actual_script_normalizes_yaml_inputs_and_finishes_through_real_entity_runtime():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, 'hold_open')
            platform, script, task, remove = None, None, None, None
            started = asyncio.Event()
            try:
                await rig.initialize()
                platform, sent, _ = await setup_memory_entity(hass, rig, started=started)
                def publish():
                    hass.states.async_set(SENSOR, rig.runtime.controller.phase, rig.runtime.stage_attributes())
                remove = rig.runtime.add_listener(publish)
                publish()
                # Preserve HA's NodeDict/NodeList/NodeStr values through actual
                # BlueprintInputs and ScriptVariables, then render the action.
                requested = parse_yaml(json.dumps([STAGE]))
                assert type(requested).__name__ == 'NodeListClass'
                script = await make_script(hass, requested)
                task = asyncio.create_task(script.async_run(context=Context()))
                await asyncio.wait_for(started.wait(), 3)
                for _ in range(5):
                    await asyncio.sleep(0)
                rig.observe('cleaning', in_cleaning=1)
                await rig.runtime.async_tick()
                begin = int(rig.runtime.session.to_dict()['sent_utc'])
                rig.summary.records, rig.summary.clean_count = [2, 1], 2
                rig.summary.last_clean_record = NS(begin=begin, end=int(rig.utc), complete=1, error=0)
                rig.observe('charging')
                await rig.runtime.async_tick()
                rig.observe('charging')
                await rig.runtime.async_tick()
                await asyncio.wait_for(task, 3)
                assert not script.is_running
                assert rig.runtime.stage_attributes()['stage_result'] == 'succeeded'
                assert sent == [(s['command'], s['params']) for s in STAGE['settings']] + [(STAGE['command'], STAGE['params'])]
                assert rig.commands == ['open_cover', 'robot_start', 'close_cover']
            finally:
                if task is not None and not task.done():
                    await script.async_stop()
                    await asyncio.gather(task, return_exceptions=True)
                if remove is not None:
                    remove()
                if platform is not None:
                    await platform.async_reset()
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


def test_native_invalid_stage_and_missing_response_are_rejected_before_transport():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, 'hold_open')
            platform = None
            try:
                await rig.initialize()
                platform, sent, _ = await setup_memory_entity(hass, rig)
                with pytest.raises(ServiceValidationError) as caught:
                    await hass.services.async_call(
                        'roborock_plus', 'start_garage_cleaning_stage',
                        {'entity_id': VACUUM, 'stage': {'command': 'app_start', 'settings': []}},
                        blocking=True, return_response=True,
                    )
                assert caught.value.translation_placeholders['reason'] == 'invalid_stage'
                with pytest.raises(HomeAssistantError):
                    await hass.services.async_call(
                        'roborock_plus', 'start_garage_cleaning_stage',
                        {'entity_id': VACUUM, 'stage': STAGE}, blocking=True,
                    )
                assert sent == rig.commands == []
            finally:
                if platform is not None:
                    await platform.async_reset()
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


@pytest.mark.parametrize('protocol', ['RoborockQ7Vacuum', 'RoborockQ10Vacuum'])
def test_native_non_v1_entity_dispatch_rejects_with_explicit_protocol_error(protocol):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, 'hold_open')
            platform = None
            try:
                await rig.initialize()
                platform, sent, _ = await setup_memory_entity(hass, rig, protocol)
                with pytest.raises(ServiceValidationError) as caught:
                    await hass.services.async_call(
                        'roborock_plus', 'start_garage_cleaning_stage',
                        {'entity_id': VACUUM, 'stage': STAGE}, blocking=True, return_response=True,
                    )
                assert caught.value.translation_placeholders['reason'] == 'unsupported_protocol'
                assert sent == rig.commands == []
            finally:
                if platform is not None:
                    await platform.async_reset()
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())
