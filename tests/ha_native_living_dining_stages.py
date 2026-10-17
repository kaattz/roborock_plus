"""Validate actual room handoff presets; no household services are called."""

import asyncio
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS

import pytest

from homeassistant.components.blueprint.models import BlueprintInputs
from homeassistant.core import Context, HomeAssistant, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script, async_validate_actions_config
from homeassistant.helpers.script_variables import ScriptVariables
from homeassistant.util.yaml import parse_yaml
from roborock.data.v1.v1_clean_modes import CleanRoutes, VacuumModes, get_water_mode_mapping

from ha_native_staged_cleaning_script import BLUEPRINT, BLUEPRINT_SCHEMA, Blueprint, MemoryStages, SENSOR, VACUUM
from custom_components.roborock_plus.garage_stage import validate_stage


EXAMPLES = Path(__file__).parents[1] / 'docs/examples'
PRESETS = (
    ('living-dining-sweep-then-mop.yaml', 'garage_living_dining_sweep_then_mop', [3, 7]),
    ('rooms-sweep-then-mop.yaml', 'garage_kitchen_sweep_then_mop', [5]),
    ('rooms-sweep-then-mop.yaml', 'garage_master_bedroom_sweep_then_mop', [1]),
    ('rooms-sweep-then-mop.yaml', 'garage_second_bedroom_sweep_then_mop', [6]),
    ('rooms-sweep-then-mop.yaml', 'garage_master_bathroom_sweep_then_mop', [2]),
    ('rooms-sweep-then-mop.yaml', 'garage_guest_bathroom_sweep_then_mop', [4]),
)


def config(filename, key):
    return parse_yaml((EXAMPLES / filename).read_text(encoding='utf-8'))[key]


@pytest.mark.parametrize('filename,key,segments', PRESETS, ids=[row[1] for row in PRESETS])
def test_preset_modes_match_library_defaults_for_the_observed_slide_water_profile(filename, key, segments):
    requested = config(filename, key)['use_blueprint']['input']
    assert requested['vacuum_entity'] == 'vacuum.g20s_ultra'
    assert requested['stage_sensor'] == 'sensor.ke_ting_sao_di_ji_garage_control_phase'
    features = NS(is_support_water_mode=True, is_water_slide_mode_supported=True,
                  is_pure_clean_mop_supported=True, is_customized_clean_supported=False,
                  is_smart_clean_mode_set_supported=False, is_clean_route_setting_supported=True)
    # Match StatusTrait/HA select's explicit mapping. The enum's shared string
    # aliases can make the generic motor helper return legacy medium=202.
    water_codes = {name: code for code, name in get_water_mode_mapping(features).items()}
    expected_modes = ((VacuumModes.BALANCED.code, water_codes['off']),
                      (VacuumModes.OFF.code, water_codes['medium']))
    for stage, (fan, water) in zip(requested['stages'], expected_modes, strict=True):
        actual = {item['command']: item['params'][0] for item in stage['settings']}
        assert actual == {'set_custom_mode': fan, 'set_water_box_custom_mode': water,
                          'set_mop_mode': CleanRoutes.STANDARD.code}
        assert stage['command'] == 'app_segment_clean'
        assert stage['params'] == [{'segments': segments, 'repeat': 1}]


@pytest.mark.parametrize('filename,key,segments', PRESETS, ids=[row[1] for row in PRESETS])
def test_actual_preset_substitution_and_script_execute_both_stages_in_memory(filename, key, segments):
    async def exercise():
        with TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            memory = MemoryStages(hass)
            instance_config = config(filename, key)
            inputs = instance_config['use_blueprint']['input']
            requested = inputs['stages']
            # Keep the actual preset parameters, replacing only physical targets.
            inputs['vacuum_entity'], inputs['stage_sensor'] = VACUUM, SENSOR
            # Use the same real blueprint with this file's actual instance inputs.
            kwargs = {'schema': BLUEPRINT_SCHEMA} if 'schema' in inspect.signature(Blueprint).parameters else {}
            blueprint = Blueprint(parse_yaml(BLUEPRINT.read_text(encoding='utf-8')), expected_domain='script', **kwargs)
            instance = BlueprintInputs(blueprint, instance_config)
            instance.validate()
            blueprint_config = instance.async_substitute()
            actions = await async_validate_actions_config(hass, cv.SCRIPT_SCHEMA(blueprint_config['sequence']))
            script = Script(hass, actions, 'Actual local preset', 'test',
                            script_mode=blueprint_config['mode'],
                            variables=ScriptVariables(cv.template_complex(blueprint_config['variables'])))
            # Memory service I/O still validates the actual production stage contract.
            original_start = memory.start
            async def checked_start(call):
                validate_stage(call.data['stage'])
                return await original_start(call)
            hass.services.async_register('roborock_plus', 'start_garage_cleaning_stage', checked_start,
                                         supports_response=SupportsResponse.ONLY)
            await asyncio.wait_for(script.async_run(context=Context()), 3)
            assert len(memory.calls) == 2
            assert [call['stage'] for call in memory.calls] == requested
            assert all(call['stage']['params'] == [{'segments': segments, 'repeat': 1}]
                       for call in memory.calls)
            assert 'previous_receipt' not in memory.calls[0]
            assert memory.calls[1]['previous_receipt'] == memory.receipts[0]
    asyncio.run(exercise())
