"""Execute the actual staged-cleaning blueprint with HA Script and memory I/O.

Explicit native runner only. All services below belong to a fresh in-memory HA;
there are no accounts, real integration setup, listeners or physical devices.
"""

import asyncio
from copy import deepcopy
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Context, HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script, _ScriptRun, async_validate_actions_config
from homeassistant.helpers.script_variables import ScriptVariables
from homeassistant.util.yaml import parse_yaml


BLUEPRINT = Path(__file__).parents[1] / "blueprints/多阶段清扫.yaml"
VACUUM = "vacuum.memory_robot"
SENSOR = "sensor.memory_garage_phase"


def stages():
    """Protocol examples are test data, never a suggested household routine."""
    return [
        {"command": "app_segment_clean", "params": [{"segments": [16], "repeat": 2}],
         "settings": [{"command": "set_custom_mode", "params": [102]},
                      {"command": "set_water_box_custom_mode", "params": [202]},
                      {"command": "set_mop_mode", "params": [300]}]},
        {"command": "app_start", "params": None,
         "settings": [{"command": "set_custom_mode", "params": [102]},
                      {"command": "set_water_box_custom_mode", "params": [202]},
                      {"command": "set_mop_mode", "params": [300]},
                      {"command": "set_clean_repeat_times", "params": {"repeat": 1}}]},
    ]


def load_blueprint(stage_list):
    inputs = {"vacuum_entity": VACUUM, "stage_sensor": SENSOR,
              "stages": stage_list, "stage_timeout_minutes": 1}

    # Use HA's real !input parser, metadata/selector validation and substitution.
    # 2026.10 added an explicit domain schema argument to Blueprint.__init__.
    kwargs = {"schema": BLUEPRINT_SCHEMA} if "schema" in inspect.signature(Blueprint).parameters else {}
    blueprint = Blueprint(parse_yaml(BLUEPRINT.read_text(encoding="utf-8")), expected_domain="script", **kwargs)
    assert not blueprint.validate()
    instance = BlueprintInputs(blueprint, {"use_blueprint": {"path": "memory.yaml", "input": inputs}})
    instance.validate()
    return instance.async_substitute()


class MemoryStages:
    """Only the external service and its published facts are test substitutes."""

    def __init__(self, hass, scenario="success", wrapped=True):
        self.hass, self.scenario, self.wrapped = hass, scenario, wrapped
        self.calls = []
        self.receipts = []
        self.entered = asyncio.Event()
        self.pending = None
        self.background = []
        self.publish(None, None, phase="idle")
        hass.states.async_set(VACUUM, "docked")
        hass.services.async_register(
            "roborock_plus", "start_garage_cleaning_stage", self.start,
            supports_response=SupportsResponse.ONLY,
        )

    def publish(self, receipt, result, *, phase="task_open", reason=None, **overrides):
        receipt = receipt or {"runtime_id": "runtime-1", "session_id": None, "interruption_revision": 0}
        attributes = {
            "stage_runtime_id": receipt["runtime_id"], "stage_session_id": receipt["session_id"],
            "stage_interruption_revision": receipt["interruption_revision"],
            "stage_result": result, "stage_reason": reason,
        }
        attributes.update(overrides)
        self.hass.states.async_set(SENSOR, phase, attributes)

    async def finish_later(self, receipt):
        # Exercise an actual wait subscription, including internal pauses that
        # must not be mistaken for a user interruption.
        for phase in ("pausing_return", "return_opening", "resuming_return", "task_open"):
            self.publish(receipt, "running", phase=phase)
            self.hass.states.async_set(VACUUM, "paused" if phase == "return_opening" else "cleaning")
            await asyncio.sleep(0)
        self.publish(receipt, "succeeded", phase="idle")
        self.hass.states.async_set(VACUUM, "docked")

    async def start(self, call):
        self.calls.append(deepcopy(dict(call.data)))
        index = len(self.calls)
        if index == 1:
            assert "previous_receipt" not in call.data
        else:
            assert call.data["previous_receipt"] == self.receipts[-1]
        if self.scenario == "setting_error":
            raise HomeAssistantError("memory setting rejected")
        receipt = {"runtime_id": "runtime-1", "session_id": f"stage-{index}", "interruption_revision": 0}
        self.receipts.append(receipt)
        self.publish(receipt, "running")
        self.entered.set()
        if index > 1 or self.scenario == "success":
            self.publish(receipt, "succeeded", phase="idle")
        elif self.scenario == "internal_pause":
            self.background.append(asyncio.create_task(self.finish_later(receipt)))
        elif self.scenario in ("cancel_then_docked", "pause_then_resume"):
            self.publish(receipt, "aborted", phase="idle" if self.scenario == "cancel_then_docked" else "manual_hold",
                         reason="cancelled" if self.scenario == "cancel_then_docked" else "user_pause",
                         stage_interruption_revision=1)
            if self.scenario == "pause_then_resume":
                self.hass.states.async_set(VACUUM, "paused")
                await asyncio.sleep(0)
            self.hass.states.async_set(VACUUM, "docked" if self.scenario == "cancel_then_docked" else "cleaning")
        elif self.scenario == "new_session":
            self.publish(receipt, "succeeded", stage_session_id="other-task")
        elif self.scenario == "reload":
            self.publish(receipt, "succeeded", stage_runtime_id="new-runtime")
        elif self.scenario == "reset":
            self.publish(receipt, "succeeded", stage_interruption_revision=1)
        elif self.scenario == "unknown":
            self.hass.states.async_set(SENSOR, "unavailable")
        elif self.scenario == "missing_attribute":
            self.publish(receipt, "succeeded", stage_runtime_id=None)
        elif self.scenario == "bool_revision":
            self.publish(receipt, "succeeded", stage_interruption_revision=False)
        elif self.scenario == "missing_result":
            self.publish(receipt, None)
        elif self.scenario in ("hold", "timeout"):
            self.pending = receipt
        else:
            raise AssertionError(self.scenario)
        return {VACUUM: receipt} if self.wrapped else receipt


async def make_script(hass, stage_list):
    config = load_blueprint(stage_list)
    sequence = await async_validate_actions_config(hass, cv.SCRIPT_SCHEMA(config["sequence"]))
    return Script(
        hass, sequence, "Memory staged cleaning", "script", script_mode=config["mode"],
        variables=ScriptVariables(cv.template_complex(config["variables"])), log_exceptions=False,
    )


@pytest.mark.parametrize("wrapped", [True, False])
@pytest.mark.parametrize("scenario", ["success", "internal_pause"])
def test_native_stages_advance_only_with_receipt_success_and_keep_exact_parameters(wrapped, scenario):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass, scenario, wrapped)
            requested = stages()
            script = await make_script(hass, requested)
            await asyncio.wait_for(script.async_run(context=Context()), 3)
            await asyncio.gather(*memory.background)
            assert len(memory.calls) == 2
            assert [call["stage"] for call in memory.calls] == requested
            assert all(call["entity_id"] == [VACUUM] for call in memory.calls)
            assert memory.calls[1]["previous_receipt"] == memory.receipts[0]
    asyncio.run(exercise())


@pytest.mark.parametrize("scenario", [
    "cancel_then_docked", "pause_then_resume", "new_session", "reload", "reset", "unknown", "missing_attribute", "bool_revision", "missing_result",
])
def test_native_abort_unknown_or_changed_receipt_never_starts_next_stage(scenario):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass, scenario)
            script = await make_script(hass, stages())
            await asyncio.wait_for(script.async_run(context=Context()), 3)
            assert len(memory.calls) == 1
            assert not script.is_running
            # Later unrelated success must not replay an ended Script run.
            memory.publish(memory.receipts[0], "succeeded", phase="idle")
            await hass.async_block_till_done()
            assert len(memory.calls) == 1
    asyncio.run(exercise())


def invalid_stages():
    valid = stages()[0]
    cases = [{**valid, "command": "app_charge"}, {**valid, "force": True},
             {**valid, "settings": "set_custom_mode"}, {**valid, "command": 1}, "app_start"]
    for mutation in ("unknown", "duplicate", "bool", "float", "missing_mode", "extra_key", "bad_repeat", "missing_repeat"):
        bad = deepcopy(valid)
        if mutation == "unknown":
            bad["settings"].append({"command": "app_pause", "params": None})
        elif mutation == "duplicate":
            bad["settings"].append(deepcopy(bad["settings"][0]))
        elif mutation in ("bool", "float"):
            bad["settings"][0]["params"] = [True if mutation == "bool" else 102.5]
        elif mutation == "missing_mode":
            bad["settings"].pop()
        elif mutation == "extra_key":
            bad["settings"][0]["force"] = True
        elif mutation == "bad_repeat":
            bad["settings"].append({"command": "set_clean_repeat_times", "params": {"repeat": True}})
        elif mutation == "missing_repeat":
            bad["command"] = "app_start"
        cases.append(bad)
    return cases


@pytest.mark.parametrize("invalid", invalid_stages())
def test_native_all_stages_are_prevalidated_before_any_service_call(invalid):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass)
            script = await make_script(hass, [stages()[0], invalid])
            await asyncio.wait_for(script.async_run(context=Context()), 3)
            assert memory.calls == []
    asyncio.run(exercise())


@pytest.mark.parametrize("params", [float("nan"), float("inf"), {"nested": [float("nan")]}, {1: "non-JSON key"}])
def test_native_non_json_later_parameters_are_rejected_before_first_stage(params):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass)
            requested = stages()
            requested[1]["params"] = params
            script = await make_script(hass, requested)
            await asyncio.wait_for(script.async_run(context=Context()), 3)
            assert memory.calls == []
    asyncio.run(exercise())


def test_native_stage_setting_error_propagates_without_scheduling_later_stage():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass, "setting_error")
            script = await make_script(hass, stages())
            with pytest.raises(HomeAssistantError, match="memory setting rejected"):
                await asyncio.wait_for(script.async_run(context=Context()), 3)
            assert len(memory.calls) == 1
    asyncio.run(exercise())


def test_native_completion_timeout_ends_run_without_replay(monkeypatch):
    original = _ScriptRun._async_futures_with_timeout

    def expire_immediately(run, timeout):
        futures, handle, future = original(run, timeout)
        assert timeout == 60
        handle.cancel()
        run._hass.loop.call_soon(lambda: None if future.done() else future.set_result(None))
        return futures, handle, future

    monkeypatch.setattr(_ScriptRun, "_async_futures_with_timeout", expire_immediately)

    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass, "timeout")
            script = await make_script(hass, stages())
            await asyncio.wait_for(script.async_run(context=Context()), 3)
            assert len(memory.calls) == 1
            memory.publish(memory.pending, "succeeded", phase="idle")
            await hass.async_block_till_done()
            assert len(memory.calls) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("cancel", [False, True, "shutdown"])
def test_native_single_mode_and_script_stop_do_not_queue_or_replay(cancel):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass, "hold")
            script = await make_script(hass, stages())
            first = asyncio.create_task(script.async_run(context=Context()))
            try:
                await asyncio.wait_for(memory.entered.wait(), 2)
                await asyncio.sleep(0)
                assert script.is_running
                if cancel == "shutdown":
                    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
                    await hass.async_block_till_done()
                elif cancel:
                    await script.async_stop()
                else:
                    await script.async_run(context=Context())
                assert len(memory.calls) == 1
                memory.publish(memory.pending, "succeeded", phase="idle")
                if cancel:
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(first, 3)
                else:
                    await asyncio.wait_for(first, 3)
                await hass.async_block_till_done()
                assert len(memory.calls) == (1 if cancel else 2)
            finally:
                if not first.done():
                    await script.async_stop()
                    await asyncio.gather(first, return_exceptions=True)
    asyncio.run(exercise())
