"""In-memory mutations of the real blueprint, checked by native Script tests.

No production files are written. Each mutant changes only YAML source supplied
to HA's real Blueprint parser; existing behavior tests remain the oracle.
"""

from copy import deepcopy
import asyncio
import json
from tempfile import TemporaryDirectory

import pytest

import ha_native_staged_cleaning_script as workflow
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template
from homeassistant.util.yaml import parse_yaml

from custom_components.roborock_plus.garage_stage import validate_stage


class MemoryBlueprint:
    def __init__(self, source):
        self.source = source

    def read_text(self, *, encoding):
        assert encoding == "utf-8"
        return self.source


def without(source, fragment, count):
    assert source.count(fragment) == count, "mutation no longer matches the actual blueprint"
    return source.replace(fragment, "")


def oracle(name):
    if name == "all_stage_preflight":
        bad = deepcopy(workflow.stages()[0])
        bad["command"] = "app_charge"
        workflow.test_native_all_stages_are_prevalidated_before_any_service_call(bad)
    else:
        scenario = {"success_only": "missing_result", "session_identity": "new_session",
                    "runtime_identity": "reload", "interruption_revision": "reset",
                    "revision_type": "bool_revision"}[name]
        workflow.test_native_abort_unknown_or_changed_receipt_never_starts_next_stage(scenario)


MUTATIONS = (
    "all_stage_preflight", "success_only", "session_identity", "runtime_identity",
    "interruption_revision", "revision_type",
)


def mutate(source, name):
    if name == "all_stage_preflight":
        fragment = "not stages_valid or "
        assert source.count(fragment) == 1
        return source.replace(fragment, "")
    if name == "success_only":
        return without(source, "or state_attr(stage_sensor, 'stage_result') != 'succeeded'", 1)
    if name == "revision_type":
        return without(source, "or state_attr(stage_sensor, 'stage_interruption_revision') is not integer", 2)
    attribute, expected = {
        "session_identity": ("stage_session_id", "session_id"),
        "runtime_identity": ("stage_runtime_id", "runtime_id"),
        "interruption_revision": ("stage_interruption_revision", "interruption_revision"),
    }[name]
    return without(source, f"or state_attr(stage_sensor, '{attribute}') != receipt.{expected}", 2)


@pytest.mark.parametrize("name", MUTATIONS)
def test_unmodified_blueprint_control_passes_existing_behavior_oracle(name):
    oracle(name)


@pytest.mark.parametrize("name", MUTATIONS)
def test_guard_removal_is_killed_by_existing_native_script_oracle(monkeypatch, name):
    source = workflow.BLUEPRINT.read_text(encoding="utf-8")
    monkeypatch.setattr(workflow, "BLUEPRINT", MemoryBlueprint(mutate(source, name)))
    with pytest.raises(AssertionError):
        oracle(name)


def test_json_stage_contract_matches_python_and_actual_jinja_preflight():
    """Compare validators on JSON-representable inputs, without moving devices."""
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            config = workflow.load_blueprint(workflow.stages())
            template = Template(config["sequence"][0]["variables"]["stages_valid"], hass)
            candidates = workflow.stages() + workflow.invalid_stages() + [None, False, 1, {}, []]
            for params in (None, {}, [], {"nested": [None, True, 1.5, "value"]},
                           float("nan"), float("inf"), {1: "invalid JSON key"}):
                candidate = deepcopy(workflow.stages()[0])
                candidate["params"] = params
                candidates.append(candidate)
            omitted = deepcopy(workflow.stages()[0])
            omitted.pop("params")
            candidates.append(omitted)
            for candidate in candidates:
                try:
                    validate_stage(candidate)
                except ValueError:
                    expected = False
                else:
                    expected = True
                actual = template.async_render({"stages": [candidate]})
                assert type(actual) is bool
                assert actual is expected, repr(candidate)
    asyncio.run(exercise())


def test_annotated_yaml_inputs_reach_python_service_validator_without_type_drift():
    """HA's annotated YAML objects must survive the actual Script data path."""
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = workflow.MemoryStages(hass)
            original = memory.start
            validated = []

            async def validate_service_input(call):
                validated.append(validate_stage(call.data["stage"]))
                return await original(call)

            from homeassistant.core import Context, SupportsResponse
            hass.services.async_register(
                "roborock_plus", "start_garage_cleaning_stage", validate_service_input,
                supports_response=SupportsResponse.ONLY,
            )
            requested = parse_yaml(json.dumps(workflow.stages()))
            script = await workflow.make_script(hass, requested)
            await asyncio.wait_for(script.async_run(context=Context()), 3)
            assert len(memory.calls) == 2
            assert validated == workflow.stages()
    asyncio.run(exercise())
