"""Execute the real staged-cleaning blueprint with HA Script and memory I/O.

Explicit native runner only. Every service below belongs to a fresh in-memory HA;
there are no accounts, real integration setup, listeners or physical devices.

The blueprint's contract changed with the position-driven rewrite: the service now
blocks until the stage is actually finished and returns its outcome, instead of
returning a receipt that the script polls for. That removed the whole class of
failures the old tests were guarding against -- a receipt that never arrives, a
revision that changes under you, a ``running`` state that never appears -- because
there is no intermediate state left to get wrong.

What still has to hold is checked here: the list is validated before anything
starts, exact parameters are passed through, and a failing stage stops the run
rather than marching on to the next room.
"""

import asyncio
from copy import deepcopy
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.core import Context, HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script, async_validate_actions_config
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
    """Only the external service is a substitute.

    ``outcomes`` decides what each successive call returns, so a test can make the
    second stage fail without pretending to be the device.
    """

    def __init__(self, hass, outcomes=None, wrapped=False, error=None):
        self.hass = hass
        self.outcomes = list(outcomes or ['succeeded'])
        self.wrapped = wrapped
        self.error = error
        self.calls = []
        hass.states.async_set(VACUUM, "docked")
        hass.states.async_set(SENSOR, "idle")
        hass.services.async_register(
            "roborock_plus", "start_garage_cleaning_stage", self.start,
            supports_response=SupportsResponse.ONLY,
        )

    async def start(self, call):
        self.calls.append(deepcopy(dict(call.data)))
        if self.error is not None:
            raise HomeAssistantError(self.error)
        index = min(len(self.calls) - 1, len(self.outcomes) - 1)
        result = self.outcomes[index]
        payload = {"runtime_id": "position-driven", "session_id": None,
                   "interruption_revision": 0, "result": result,
                   "reason": None if result == 'succeeded' else 'stage_failed'}
        return {VACUUM: payload} if self.wrapped else payload


async def make_script(hass, stage_list):
    """Build the real Script from the blueprint, the way Home Assistant would."""
    config = load_blueprint(stage_list)
    sequence = await async_validate_actions_config(hass, cv.SCRIPT_SCHEMA(config["sequence"]))
    return Script(
        hass, sequence, "Memory staged cleaning", "script",
        script_mode=config["mode"],
        variables=ScriptVariables(cv.template_complex(config["variables"])),
        log_exceptions=False,
    )


# --- the outcome decides whether the run continues ---------------------------

def test_native_a_successful_stage_advances_to_the_next_one():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass, outcomes=['succeeded', 'succeeded'])
            script = await make_script(hass, stages())
            await script.async_run(context=Context())
            assert len(memory.calls) == 2, 'both stages should run'

    asyncio.run(exercise())


@pytest.mark.parametrize("wrapped", [False, True])
def test_native_exact_stage_parameters_reach_the_service_unchanged(wrapped):
    """The service must receive the caller's stage verbatim, not a normalised copy."""
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass, outcomes=['succeeded', 'succeeded'], wrapped=wrapped)
            requested = stages()
            script = await make_script(hass, requested)
            await script.async_run(context=Context())
            for position, expected in enumerate(requested):
                assert memory.calls[position]["stage"] == expected
            assert all("previous_receipt" not in call for call in memory.calls), (
                'the retired receipt protocol must not reappear')

    asyncio.run(exercise())


def test_native_a_failed_stage_stops_the_run():
    """A stage nobody could confirm must not be followed by another room.

    ``stop: error: true`` marks the run as failed but does not raise out of
    ``async_run``, so the observable fact is that the second stage never reaches
    the service.
    """
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass, outcomes=['failed', 'succeeded'])
            script = await make_script(hass, stages())
            await script.async_run(context=Context())
            assert len(memory.calls) == 1, 'the second stage must not start'
            assert not script.is_running

    asyncio.run(exercise())


def test_native_a_service_error_stops_the_run():
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass, error="memory setting rejected")
            script = await make_script(hass, stages())
            with pytest.raises(HomeAssistantError, match="memory setting rejected"):
                await script.async_run(context=Context())
            assert len(memory.calls) == 1

    asyncio.run(exercise())


# --- prevalidation ------------------------------------------------------------

def invalid_stages():
    cases = []
    for mutation in ("empty", "not_list", "bad_command", "bad_settings", "duplicate",
                     "bool", "float", "missing_mode", "extra_key", "bad_repeat",
                     "missing_repeat"):
        bad = deepcopy(stages()[0])
        if mutation == "empty":
            bad["params"] = []
        elif mutation == "not_list":
            bad["params"] = {"segments": [1]}
        elif mutation == "bad_command":
            bad["command"] = "app_pause"
        elif mutation == "bad_settings":
            bad["settings"] = "nope"
        elif mutation == "duplicate":
            bad["settings"].append(deepcopy(bad["settings"][0]))
        elif mutation in ("bool", "float"):
            bad["settings"][0]["params"] = [True if mutation == "bool" else 102.5]
        elif mutation == "missing_mode":
            bad["settings"].pop()
        elif mutation == "extra_key":
            bad["settings"][0]["force"] = True
        elif mutation == "bad_repeat":
            bad["settings"].append({"command": "set_clean_repeat_times",
                                    "params": {"repeat": True}})
        elif mutation == "missing_repeat":
            bad["command"] = "app_start"
        cases.append(bad)
    return cases


@pytest.mark.parametrize("invalid", invalid_stages())
def test_native_all_stages_are_prevalidated_before_any_service_call(invalid):
    """The whole list is checked up front, so a bad stage 2 never starts stage 1."""
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass)
            script = await make_script(hass, [stages()[0], invalid])
            await asyncio.wait_for(script.async_run(context=Context()), 3)
            assert memory.calls == []

    asyncio.run(exercise())


@pytest.mark.parametrize("params", [float("nan"), float("inf"),
                                    {"nested": [float("nan")]}, {1: "non-JSON key"}])
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


def test_native_an_unavailable_stage_sensor_refuses_to_start():
    """The sensor only proves the integration is loaded; unknown means it is not."""
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass)
            hass.states.async_set(SENSOR, "unavailable")
            script = await make_script(hass, stages())
            await asyncio.wait_for(script.async_run(context=Context()), 3)
            assert memory.calls == []

    asyncio.run(exercise())
