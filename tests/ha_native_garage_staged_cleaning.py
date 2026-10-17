"""Stage receipts on real HA; all covers and robots are in-memory services."""

import asyncio
from copy import deepcopy
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS

import pytest
from homeassistant.core import HomeAssistant

from ha_native_garage_feedback_recovery import NativeFeedbackRecovery
from ha_native_garage_after_exit_runtime import NativeAfterExitBoundary


STAGE = {'command': 'app_segment_clean', 'params': [{'segments': [16], 'repeat': 2}],
         'settings': [{'command': 'set_custom_mode', 'params': [103]},
                      {'command': 'set_water_box_custom_mode', 'params': [202]},
                      {'command': 'set_mop_mode', 'params': [300]}]}


@pytest.mark.parametrize('mode', ['hold_open', 'after_exit'])
def test_native_stage_opens_fully_and_completes_only_its_successful_record(mode):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, mode)
            sent = []
            async def send(command, params):
                sent.append((command, deepcopy(params), rig.now))
                if command == STAGE['command']:
                    return await rig.start_robot()
                return 'accepted'
            try:
                await rig.initialize()
                receipt = await rig.runtime.async_start_stage(STAGE, send)
                assert receipt['session_id'] == rig.runtime.session.session_id
                assert sent[0][2] - rig.events[0][1] == 45
                assert [command for command, _, _ in sent] == [s['command'] for s in STAGE['settings']] + [STAGE['command']]
                rig.observe('cleaning', in_cleaning=1)
                await rig.runtime.async_tick()
                rig.observe('charging')
                await rig.runtime.async_tick()
                rig.observe('charging')
                await rig.runtime.async_tick()
                assert rig.runtime.stage_attributes()['stage_result'] == 'running'
                begin = int(rig.runtime.session.to_dict()['sent_utc'])
                rig.summary.records, rig.summary.clean_count = [2, 1], 2
                rig.summary.last_clean_record = NS(begin=begin, end=int(rig.utc), complete=1, error=0)
                rig.observe('charging')
                await rig.runtime.async_tick()
                await hass.async_block_till_done()
                assert rig.runtime.stage_attributes()['stage_result'] == 'succeeded'
                assert rig.runtime.stage_attributes()['stage_session_id'] == receipt['session_id']
                assert rig.commands[-1] == 'close_cover'
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


@pytest.mark.parametrize('mode', ['hold_open', 'after_exit'])
def test_native_user_pause_during_setting_aborts_before_cleaning(mode):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, mode)
            sent = []
            async def pause():
                rig.observe('paused')
            async def send(command, params):
                sent.append(command)
                await rig.runtime.async_send('app_pause', pause)
            try:
                await rig.initialize()
                with pytest.raises(ValueError):
                    await rig.runtime.async_start_stage(STAGE, send)
                assert sent == ['set_custom_mode']
                assert 'robot_start' not in rig.commands
                assert rig.runtime.stage_attributes()['stage_result'] == 'aborted'
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


@pytest.mark.parametrize('mode', ['hold_open', 'after_exit'])
def test_native_external_observed_pause_stays_aborted_after_resume(mode):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            rig = NativeFeedbackRecovery(hass, mode)
            async def send(command, params):
                if command == STAGE['command']:
                    return await rig.start_robot()
                return 'accepted'
            try:
                await rig.initialize()
                receipt = await rig.runtime.async_start_stage(STAGE, send)
                rig.observe('cleaning', in_cleaning=1)
                await rig.runtime.async_tick()
                rig.observe('paused')
                await rig.runtime.async_tick()
                assert rig.runtime.stage_attributes()['stage_result'] == 'aborted'
                rig.observe('cleaning', in_cleaning=1)
                await rig.runtime.async_tick()
                assert rig.runtime.stage_attributes()['stage_result'] == 'aborted'
                with pytest.raises(ValueError):
                    await rig.runtime.async_start_stage(STAGE, send, previous_receipt=receipt)
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())


def test_native_owned_return_pause_preserves_the_stage_until_real_completion():
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
            async def send(command, params):
                if command == STAGE['command']:
                    rig.commands.append('robot_start')
                    rig.position('cleaning')
                return 'accepted'
            try:
                await rig.initialize()
                receipt = await rig.runtime.async_start_stage(STAGE, send)
                rig.position('cleaning', point=(20, 20))
                await rig.tick()
                assert rig.runtime.controller.phase == 'outside_closed'
                rig.position('returning_home', in_returning=1, point=(20, 20))
                await rig.tick()
                assert rig.commands[-3:] == ['app_pause', 'open_cover', 'app_resume']
                assert paused_results == ['running']
                assert rig.runtime.stage_attributes()['stage_result'] == 'running'
                assert rig.runtime.stage_attributes()['stage_session_id'] == receipt['session_id']
                rig.position('charging', in_cleaning=0)
                await rig.tick()
                rig.position('charging', in_cleaning=0)
                await rig.tick()
                assert rig.runtime.stage_attributes()['stage_result'] == 'running'
            finally:
                rig.runtime.shutdown()
                await hass.async_block_till_done()
    asyncio.run(exercise())
