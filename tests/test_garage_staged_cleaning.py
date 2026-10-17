"""Stage receipts exercise production runtime with in-memory boundaries."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from test_garage_runtime import Boundaries


class StageRig(Boundaries):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.runtime = self.create()
        self.runtime.schedule = lambda: None
        self.runtime.binding["close_permission_entity"] = "input_boolean.close"
        self.states["input_boolean.close"] = NS(state="off", attributes={})
        self.coord.properties_api.clean_summary = NS(refresh=self.refresh_summary)

    async def refresh_summary(self):
        pass

    async def initialize(self):
        await self.runtime.async_restore()
        await self.runtime.async_tick()
        self.advance()
        await self.runtime.async_tick()

    async def send(self, command, params):
        self.sent.append((command, deepcopy(params)))
        return "accepted"

    async def poll(self, state, cleaning=0, returning=0):
        self.status.state.name = state
        self.status.in_cleaning, self.status.in_returning = cleaning, returning
        self.advance()
        await self.runtime.async_tick()

    async def complete(self):
        await self.poll("cleaning", 1)
        begin = int(self.runtime.session.to_dict()["sent_utc"])
        self.summary.records = [2, 1]
        self.summary.clean_count = 2
        self.summary.last_clean_record = NS(begin=begin, end=int(self.utc), complete=1, error=0)
        await self.poll("charging")
        await self.poll("charging")


STAGE = {"command": "app_segment_clean", "params": [{"segments": [16], "repeat": 2}],
         "settings": [{"command": "set_custom_mode", "params": [103]},
                      {"command": "set_water_box_custom_mode", "params": [202]},
                      {"command": "set_mop_mode", "params": [300]}]}


def test_stage_receipt_and_success_are_tied_to_the_actual_session():
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        receipt = await rig.runtime.async_start_stage(STAGE, rig.send)
        assert receipt["session_id"] == rig.runtime.session.session_id
        assert rig.sent == [(setting['command'], setting['params']) for setting in STAGE['settings']] + [("app_segment_clean", STAGE["params"])]
        assert rig.runtime.stage_attributes()["stage_result"] == "running"
        await rig.poll("charging")
        await rig.poll("charging")
        assert rig.runtime.stage_attributes()["stage_result"] == "running"
        await rig.complete()
        assert rig.runtime.stage_attributes()["stage_result"] == "succeeded"
        next_receipt = await rig.runtime.async_start_stage(STAGE, rig.send, previous_receipt=receipt)
        assert next_receipt["session_id"] != receipt["session_id"]
    asyncio.run(exercise())


@pytest.mark.parametrize("bad", [
    {"command": "app_charge"}, {"command": "app_start", "unknown": 1},
    {"command": "app_start", "settings": [{"command": "app_pause"}]},
    {"command": "app_start", "params": float("nan")},
    {"command": "app_start", "settings": "set_custom_mode"},
])
def test_invalid_stage_fails_before_any_setting_or_motion(bad):
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        with pytest.raises(ValueError):
            await rig.runtime.async_start_stage(bad, rig.send)
        assert not rig.sent
        assert rig.runtime.session.session_id is None
    asyncio.run(exercise())


@pytest.mark.parametrize("command", ["app_pause", "app_stop"])
def test_user_interrupt_cannot_be_erased_by_later_success(command):
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        receipt = await rig.runtime.async_start_stage(STAGE, rig.send)
        await rig.poll("cleaning", 1)
        await rig.runtime.async_send(command, lambda: rig.send(command, None))
        assert rig.runtime.stage_attributes()["stage_result"] == "aborted"
        await rig.complete()
        assert rig.runtime.stage_attributes()["stage_result"] == "aborted"
        with pytest.raises(ValueError):
            await rig.runtime.async_start_stage(STAGE, rig.send, previous_receipt=receipt)
    asyncio.run(exercise())


@pytest.mark.parametrize("boundary", ["summary", "setting"])
def test_pause_during_external_await_prevents_the_actual_start(boundary):
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        async def interrupt():
            await rig.runtime.async_send("app_pause", lambda: rig.send("app_pause", None))
        if boundary == "summary":
            rig.coord.properties_api.clean_summary.refresh = interrupt
        async def send(command, params):
            await rig.send(command, params)
            if boundary == "setting" and command == "set_custom_mode":
                await interrupt()
        with pytest.raises(ValueError):
            await rig.runtime.async_start_stage(STAGE, send)
        assert all(command != "app_segment_clean" for command, _ in rig.sent)
        assert rig.runtime.stage_attributes()["stage_result"] == "aborted"
    asyncio.run(exercise())


def test_concurrent_preparing_stage_cannot_overwrite_the_first():
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait_summary():
            entered.set()
            await release.wait()
        rig.coord.properties_api.clean_summary.refresh = wait_summary
        task = asyncio.create_task(rig.runtime.async_start_stage(STAGE, rig.send))
        await entered.wait()
        with pytest.raises(ValueError):
            await rig.runtime.async_start_stage(STAGE, rig.send)
        release.set()
        receipt = await task
        assert receipt["session_id"] == rig.runtime.session.session_id
        assert [command for command, _ in rig.sent].count("app_segment_clean") == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["reset", "shutdown", "fault", "paused", "replace"])
def test_old_receipt_is_revoked_by_interruption_and_session_changes(action):
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        receipt = await rig.runtime.async_start_stage(STAGE, rig.send)
        await rig.poll("cleaning", 1)
        if action == "reset":
            rig.runtime.reset()
        elif action == "shutdown":
            rig.runtime.shutdown()
        elif action == "fault":
            rig.runtime.controller.fault("feedback_unavailable")
        elif action == "paused":
            await rig.poll("paused")
        else:
            rig.runtime.session.session_id = "other-task"
        assert rig.runtime.stage_attributes()["stage_result"] == "aborted"
        with pytest.raises(ValueError):
            await rig.runtime.async_start_stage(STAGE, rig.send, previous_receipt=receipt)
    asyncio.run(exercise())


def test_controlled_return_pause_does_not_abort_the_stage():
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        await rig.runtime.async_start_stage(STAGE, rig.send)
        await rig.poll("cleaning", 1)
        rig.runtime.controller._pause_session = rig.runtime.session.session_id
        rig.runtime.controller.phase = "return_opening"
        await rig.poll("paused")
        assert rig.runtime.stage_attributes()["stage_result"] == "running"
    asyncio.run(exercise())


def test_setting_failure_does_not_send_cleaning_or_produce_a_success_receipt():
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        async def fail(command, params):
            await rig.send(command, params)
            raise RuntimeError("setting rejected")
        with pytest.raises(RuntimeError, match="setting rejected"):
            await rig.runtime.async_start_stage(STAGE, fail)
        assert rig.sent == [("set_custom_mode", [103])]
        assert rig.runtime.stage_attributes()["stage_result"] == "aborted"
    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["missing", "duplicate", "bool", "repeat", "unhashable"])
def test_settings_are_complete_unique_and_typed_before_send(change):
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        stage = deepcopy(STAGE)
        if change == "missing":
            stage['settings'].pop()
        elif change == "duplicate":
            stage['settings'].append(deepcopy(stage['settings'][0]))
        elif change == "bool":
            stage['settings'][0]['params'] = [True]
        elif change == "repeat":
            stage['command'] = 'app_start'
        else:
            stage['command'] = []
        with pytest.raises(ValueError, match='invalid_stage'):
            await rig.runtime.async_start_stage(stage, rig.send)
        assert not rig.sent
    asyncio.run(exercise())


def test_app_start_repeat_object_and_json_payload_are_preserved_without_aliases():
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        stage = deepcopy(STAGE)
        stage['command'] = 'app_start'
        stage['settings'].append({'command': 'set_clean_repeat_times', 'params': {'repeat': 3}})
        expected = deepcopy(stage)
        async def send(command, params):
            await rig.send(command, params)
            if command == 'set_custom_mode':
                stage['params'][0]['segments'].append(99)
        await rig.runtime.async_start_stage(stage, send)
        assert rig.sent[-2:] == [('set_clean_repeat_times', {'repeat': 3}), ('app_start', expected['params'])]
    asyncio.run(exercise())


def test_pause_in_the_store_await_cannot_transfer_the_new_session_to_an_old_request():
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        original = rig.store.async_save
        async def save(duid, payload):
            await rig.runtime.async_send('app_pause', lambda: rig.send('app_pause', None))
            await original(duid, payload)
        rig.store.async_save = save
        with pytest.raises(ValueError):
            await rig.runtime.async_start_stage(STAGE, rig.send)
        assert rig.sent == [('app_pause', None)]
        assert rig.runtime.stage_attributes()['stage_result'] == 'aborted'
    asyncio.run(exercise())


@pytest.mark.parametrize('failure', ['incomplete', 'error', 'old_record', 'multiple_records'])
def test_only_a_single_successful_new_record_can_complete_a_stage(failure):
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        await rig.runtime.async_start_stage(STAGE, rig.send)
        await rig.poll('cleaning', 1)
        begin = int(rig.runtime.session.to_dict()['sent_utc'])
        rig.summary.records = [2, 1]
        rig.summary.clean_count = 2
        rig.summary.last_clean_record = NS(begin=begin, end=int(rig.utc), complete=1, error=0)
        if failure == 'incomplete':
            rig.summary.last_clean_record.complete = 0
        elif failure == 'error':
            rig.summary.last_clean_record.error = 4
        elif failure == 'old_record':
            rig.summary.last_clean_record.begin = 500
        else:
            rig.summary.records = [3, 2, 1]
            rig.summary.clean_count = 3
        await rig.poll('charging')
        await rig.poll('charging')
        assert rig.runtime.stage_attributes()['stage_result'] != 'succeeded'
    asyncio.run(exercise())


def test_user_pause_after_success_still_revokes_the_next_stage_receipt():
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        receipt = await rig.runtime.async_start_stage(STAGE, rig.send)
        await rig.complete()
        assert rig.runtime.stage_attributes()['stage_result'] == 'succeeded'
        await rig.runtime.async_send('app_pause', lambda: rig.send('app_pause', None))
        with pytest.raises(ValueError, match='invalid_stage_receipt'):
            await rig.runtime.async_start_stage(STAGE, rig.send, previous_receipt=receipt)
    asyncio.run(exercise())


def test_reload_epoch_rejects_an_old_successful_receipt():
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        receipt = await rig.runtime.async_start_stage(STAGE, rig.send)
        await rig.complete()
        other = StageRig()
        await other.initialize()
        with pytest.raises(ValueError, match='invalid_stage_receipt'):
            await other.runtime.async_start_stage(STAGE, other.send, previous_receipt=receipt)
        assert not other.sent
    asyncio.run(exercise())


def test_external_task_completed_between_polls_revokes_the_previous_checkpoint():
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        receipt = await rig.runtime.async_start_stage(STAGE, rig.send)
        await rig.complete()
        before = list(rig.sent)
        async def different_summary():
            rig.summary.records, rig.summary.clean_count = [3, 2, 1], 3
            rig.summary.last_clean_record = NS(begin=int(rig.utc), end=int(rig.utc), complete=1, error=0)
        rig.coord.properties_api.clean_summary.refresh = different_summary
        with pytest.raises(ValueError):
            await rig.runtime.async_start_stage(STAGE, rig.send, previous_receipt=receipt)
        assert rig.sent == before
        assert rig.runtime.stage_attributes()['stage_result'] == 'aborted'
    asyncio.run(exercise())


def test_foreign_cleaning_observed_during_settings_cannot_be_adopted_as_our_start():
    async def exercise():
        rig = StageRig()
        await rig.initialize()
        async def send(command, params):
            await rig.send(command, params)
            if command == 'set_custom_mode':
                rig.status.state.name, rig.status.in_cleaning = 'cleaning', 1
                rig.advance()
        with pytest.raises(ValueError):
            await rig.runtime.async_start_stage(STAGE, send)
        assert rig.sent == [('set_custom_mode', [103])]
    asyncio.run(exercise())
