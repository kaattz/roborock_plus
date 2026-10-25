"""The controlled-exit switch must be reachable from the options UI.

The feature is useless if it can only be enabled by editing .storage by hand. It
also must not nag: the three waypoint numbers are only asked for once the switch
is on, because an off feature should not demand values nobody will use.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

from test_options_ui import _normal_options, _options_handler, _page_keys


def timing_step(values):
    handler = _options_handler()
    handler._garage_draft = dict(values)
    return asyncio.run(handler.async_step_garage_timing())


BASE = {
    'enabled': True,
    'task_door_mode': 'after_exit',
    'cover_entity_id': 'cover.garage',
    'open_source': 'contact',
    'close_source': 'contact',
    'open_contact': 'binary_sensor.open',
    'open_contact_state': 'on',
    'close_contact': 'binary_sensor.closed',
    'close_contact_state': 'on',
    'contact_settle_seconds': 1,
    'handover_confirmed': True,
}


def test_the_switch_is_offered_on_the_timing_step():
    page = timing_step(BASE)
    assert 'controlled_exit_enabled' in _page_keys(page)


def test_the_waypoint_numbers_are_hidden_while_the_switch_is_off():
    """An off feature must not demand three numbers."""
    page = timing_step(BASE)
    keys = _page_keys(page)
    assert 'controlled_exit_x' not in keys
    assert 'controlled_exit_y' not in keys
    assert 'controlled_exit_timeout_seconds' not in keys


def test_the_waypoint_numbers_appear_once_the_switch_is_on():
    page = timing_step({**BASE, 'controlled_exit_enabled': True})
    keys = _page_keys(page)
    assert {'controlled_exit_x', 'controlled_exit_y',
            'controlled_exit_timeout_seconds'} <= keys


def test_the_defaults_describe_the_measured_geometry():
    page = timing_step({**BASE, 'controlled_exit_enabled': True})
    defaults = {}
    for marker, _selector in page['data_schema'].items():
        key = marker.key
        if key.startswith('controlled_exit'):
            defaults[key] = marker.description.get('suggested_value', marker.default)
    assert defaults.get('controlled_exit_x') == 25696
    assert defaults.get('controlled_exit_y') == 26500
    assert defaults.get('controlled_exit_timeout_seconds') == 120


def test_saved_values_win_over_the_defaults():
    page = timing_step({**BASE, 'controlled_exit_enabled': True,
                        'controlled_exit_x': 25000, 'controlled_exit_y': 26000,
                        'controlled_exit_timeout_seconds': 60})
    values = {}
    for marker, _selector in page['data_schema'].items():
        key = marker.key
        if key.startswith('controlled_exit'):
            values[key] = marker.description.get('suggested_value', marker.default)
    assert values['controlled_exit_x'] == 25000
    assert values['controlled_exit_y'] == 26000
    assert values['controlled_exit_timeout_seconds'] == 60


def test_every_label_exists_in_all_three_translation_files():
    from test_options_ui import CONFIG_FLOW
    for name in ('strings.json', 'translations/en.json', 'translations/zh-Hans.json'):
        step = json.loads((CONFIG_FLOW.parent / name).read_text(encoding='utf-8'))[
            'options']['step']['garage_timing']
        for key in ('controlled_exit_enabled', 'controlled_exit_x',
                    'controlled_exit_y', 'controlled_exit_timeout_seconds'):
            assert key in step['data'], f'{name}: no label for {key}'
            assert key in step['data_description'], f'{name}: no description for {key}'
