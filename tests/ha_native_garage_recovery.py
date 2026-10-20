"""Recovery regressions on the existing complete, memory-only HA fixture.

Run explicitly with Home Assistant installed. No real integrations, accounts,
network clients or physical devices are loaded.
"""

import asyncio
from tempfile import TemporaryDirectory

import pytest
from homeassistant.core import HomeAssistant

from ha_native_garage_after_exit_runtime import NativeAfterExitBoundary
from ha_native_garage_staged_cleaning import STAGE


async def _start_stage(rig):
    async def send(command, params):
        if command == STAGE['command']:
            rig.commands.append('robot_start')
            rig.position('cleaning')
        return 'accepted'

    await rig.initialize()
    receipt = await rig.runtime.async_start_stage(STAGE, send)
    rig.position('cleaning', point=(20, 20))
    await rig.tick()
    assert rig.runtime.controller.phase == 'outside_closed'
    return receipt


@pytest.mark.parametrize('manual', [False, True])
def test_native_latched_fault_or_manual_hold_brakes_without_opening_or_resuming(manual):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeAfterExitBoundary(hass)
            try:
                await _start_stage(rig)
                reason = 'manual_override' if manual else 'observation_timeout'
                rig.runtime.controller.fault(reason, manual=manual)
                before = list(rig.commands)
                rig.position('returning_home', in_returning=1, point=(20, 20))
                await rig.tick()
                assert rig.commands == before + ['app_pause']
                assert rig.status.state.name == 'paused'
                assert rig.runtime.controller.reason == reason
                assert rig.runtime.controller.phase == ('manual_hold' if manual else 'blocked')
                assert rig.runtime.stage_attributes()['stage_result'] == 'aborted'
                for _ in range(3):
                    rig.position('paused', in_cleaning=0, point=(20, 20))
                    await rig.tick()
                assert rig.commands == before + ['app_pause']
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


def test_native_closed_clutch_stop_preserves_the_original_fault_and_stage_revision():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeAfterExitBoundary(hass)
            try:
                await _start_stage(rig)
                rig.runtime.controller.fault('observation_timeout')
                before = rig.runtime.stage_attributes()
                operation = rig.runtime.controller.operation_id
                commands = list(rig.commands)
                await hass.services.async_call(
                    'cover', 'stop_cover', {'entity_id': 'cover.memory_garage'}, blocking=True,
                )
                await hass.async_block_till_done()
                assert rig.commands == commands + ['stop_cover']
                assert rig.runtime.controller.reason == 'observation_timeout'
                assert rig.runtime.controller.phase == 'blocked'
                assert rig.runtime.controller.operation_id == operation
                after = rig.runtime.stage_attributes()
                assert after['stage_result'] == 'aborted'
                assert after['stage_interruption_revision'] == before['stage_interruption_revision']
                assert after['stage_reason'] == before['stage_reason']
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


def test_native_short_outside_feedback_gap_recovers_and_owned_pause_keeps_stage_running():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeAfterExitBoundary(hass)
            paused_results = []

            async def refresh():
                rig.runtime.controller.refresh()
                if rig.status.state.name == 'paused':
                    paused_results.append(rig.runtime.stage_attributes()['stage_result'])

            rig.coordinator.async_refresh = refresh
            try:
                receipt = await _start_stage(rig)
                before = list(rig.commands)
                rig.coordinator.last_update_success = False
                await rig.tick()
                assert rig.runtime.controller.reason is None
                assert rig.commands == before
                assert rig.runtime.stage_attributes()['stage_result'] == 'running'
                rig.coordinator.last_update_success = True
                rig.position('returning_home', in_returning=1, point=(20, 20))
                await rig.tick()
                assert rig.commands == before + ['app_pause', 'open_cover', 'app_resume']
                assert paused_results == ['running']
                assert rig.runtime.controller.reason is None
                stage = rig.runtime.stage_attributes()
                assert stage['stage_result'] == 'running'
                assert stage['stage_session_id'] == receipt['session_id']
                assert stage['stage_interruption_revision'] == receipt['interruption_revision']
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


def test_native_invalid_return_authority_is_rejected_after_braking_before_open():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeAfterExitBoundary(hass)
            try:
                await _start_stage(rig)
                before = list(rig.commands)
                rig.summary.records, rig.summary.clean_count = [99], 99
                rig.position('returning_home', in_returning=1, point=(20, 20))
                await rig.tick()
                assert rig.commands == before + ['app_pause']
                assert rig.runtime.controller.reason == 'session_recovery_required'
                assert rig.runtime.controller.phase == 'blocked'
                assert rig.runtime.stage_attributes()['stage_result'] == 'aborted'
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


def test_native_scheduled_return_brakes_while_an_older_stop_rpc_is_still_pending():
    """Coordinator scheduling must reach the brake without waiting for STOP.

    The pause RPC is held too, so releasing the obsolete close worker proves
    that its cleanup cannot release the newer safety operation's ownership.
    """
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeAfterExitBoundary(hass)
            stop_entered, stop_release = asyncio.Event(), asyncio.Event()
            pause_entered, pause_release = asyncio.Event(), asyncio.Event()
            old_update = None
            try:
                await rig.begin()
                before = list(rig.commands)
                original_send = rig.coordinator.properties_api.command.send

                async def close_service(call):
                    rig.commands.append(call.service)
                    hass.states.async_set(
                        'cover.memory_garage', 'closing',
                        {'supported_features': 11, 'current_position': 50}, context=call.context,
                    )
                    hass.states.async_set('binary_sensor.memory_open', 'off', context=call.context)
                    hass.states.async_set('binary_sensor.memory_closed', 'off', context=call.context)
                    rig.coordinator.last_update_success = False

                async def stop_service(call):
                    rig.commands.append(call.service)
                    stop_entered.set()
                    await stop_release.wait()

                async def robot_send(command):
                    response = await original_send(command)
                    if command == 'app_pause':
                        pause_entered.set()
                        await pause_release.wait()
                    return response

                hass.services.async_register('cover', 'close_cover', close_service)
                hass.services.async_register('cover', 'stop_cover', stop_service)
                rig.coordinator.properties_api.command.send = robot_send
                rig.position('cleaning', point=(20, 20))
                rig.runtime.schedule()
                old_update = rig.runtime._update_task
                await asyncio.wait_for(stop_entered.wait(), 1)
                assert old_update is not None and not old_update.done()
                close_owner = rig.runtime.controller._pending_operation

                rig.coordinator.last_update_success = True
                rig.position('returning_home', in_returning=1, point=(20, 20))
                # This is the exact coordinator/timer entry; do not directly
                # call controller.async_update(), which would hide the bug.
                rig.runtime.schedule()
                await asyncio.wait_for(pause_entered.wait(), .5)
                assert not stop_release.is_set()
                assert rig.commands == before + ['close_cover', 'stop_cover', 'app_pause']
                brake_owner = rig.runtime.controller._pending_operation
                assert brake_owner is not None and brake_owner != close_owner
                assert rig.runtime.controller._pending

                stop_release.set()
                await asyncio.wait_for(asyncio.shield(old_update), 1)
                assert rig.runtime.controller._pending
                assert rig.runtime.controller._pending_operation == brake_owner
                assert rig.commands == before + ['close_cover', 'stop_cover', 'app_pause']
                pause_release.set()
                await hass.async_block_till_done()
                assert not rig.runtime.controller._pending
                assert rig.runtime.controller.reason is not None
                assert rig.commands == before + ['close_cover', 'stop_cover', 'app_pause']
            finally:
                stop_release.set()
                pause_release.set()
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


def test_native_reset_waits_for_the_obsolete_stop_rpc_after_safety_pause_finishes():
    """An explicit reset cannot authorize movement over an unfinished door RPC."""
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeAfterExitBoundary(hass)
            stop_entered, stop_release = asyncio.Event(), asyncio.Event()
            pause_sent = asyncio.Event()
            try:
                await rig.begin()
                before = list(rig.commands)
                original_send = rig.coordinator.properties_api.command.send

                async def close_service(call):
                    rig.commands.append(call.service)
                    hass.states.async_set(
                        'cover.memory_garage', 'closing',
                        {'supported_features': 11, 'current_position': 50}, context=call.context,
                    )
                    hass.states.async_set('binary_sensor.memory_open', 'off', context=call.context)
                    hass.states.async_set('binary_sensor.memory_closed', 'off', context=call.context)
                    rig.coordinator.last_update_success = False

                async def stop_service(call):
                    rig.commands.append(call.service)
                    stop_entered.set()
                    await stop_release.wait()

                async def robot_send(command):
                    result = await original_send(command)
                    if command == 'app_pause':
                        pause_sent.set()
                    return result

                hass.services.async_register('cover', 'close_cover', close_service)
                hass.services.async_register('cover', 'stop_cover', stop_service)
                rig.coordinator.properties_api.command.send = robot_send
                rig.position('cleaning', point=(20, 20))
                rig.runtime.schedule()
                old_update = rig.runtime._update_task
                await asyncio.wait_for(stop_entered.wait(), 1)
                rig.coordinator.last_update_success = True
                rig.position('returning_home', in_returning=1, point=(20, 20))
                rig.runtime.schedule()
                await asyncio.wait_for(pause_sent.wait(), 1)
                await asyncio.wait_for(asyncio.shield(rig.runtime._safety_task), 1)
                assert rig.status.state.name == 'paused'
                assert old_update is not None and not old_update.done()
                assert not stop_release.is_set()
                # Fresh endpoint feedback is available even though the
                # outstanding STOP RPC has not acknowledged or unwound.
                rig.set_door(False)
                rig.runtime.controller.refresh()
                fault = rig.runtime.controller.reason
                assert fault is not None
                with pytest.raises(ValueError, match='operation_in_progress'):
                    rig.runtime.reset()
                assert rig.runtime.controller.reason == fault
                assert rig.commands == before + ['close_cover', 'stop_cover', 'app_pause']

                stop_release.set()
                await asyncio.wait_for(asyncio.shield(old_update), 1)
                await hass.async_block_till_done()
                assert rig.runtime.controller.reason == fault
                rig.runtime.reset()
                assert rig.runtime.controller.reason is None
                assert rig.runtime.controller.phase == 'manual_hold'
                assert rig.commands == before + ['close_cover', 'stop_cover', 'app_pause']
            finally:
                stop_release.set()
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())
