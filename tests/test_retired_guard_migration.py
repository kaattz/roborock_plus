"""Retiring the flat garage guard must not lose a household's protection.

The old options were ``garage_guard_enabled`` / ``garage_door_entity_id``. The
module that consumed them is deleted, so the keys are removed on setup; but a
household that had a door protected must come out the other side with a binding
naming that door.

The migration deliberately produces a **disabled** binding. The flat pair never
carried evidence sources, travel times, settle windows or a handover
acknowledgement, so filling those in would assert safety facts nobody measured.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest


def load_migration():
    """Pull the migration coroutine out of __init__ without importing Home Assistant."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "custom_components/roborock_plus/__init__.py"
    source = path.read_text(encoding="utf-8")
    start = source.index("LEGACY_GUARD_ENABLED = ")
    end = source.index("async def async_setup_entry(")
    namespace = {
        'Mapping': __import__('collections.abc', fromlist=['Mapping']).Mapping,
        'Any': __import__('typing').Any,
        'CONF_GARAGE_BINDINGS': 'garage_bindings',
        '_LOGGER': __import__('logging').getLogger('migration'),
    }
    exec(compile(source[start:end], str(path), 'exec'), namespace)
    return namespace


def entry_with(options):
    entry = NS(entry_id='entry', options=dict(options))
    return entry


def run(hass, entry):
    migration = load_migration()
    asyncio.run(migration['_async_migrate_retired_guard'](hass, entry))


class Hass:
    def __init__(self):
        self.updated = []

    @property
    def config_entries(self):
        outer = self

        class Entries:
            def async_update_entry(self, entry, *, options):
                entry.options = dict(options)
                outer.updated.append(dict(options))
        return Entries()


# --- keys are always removed ------------------------------------------------

def test_the_retired_keys_disappear_even_when_disabled():
    hass, entry = Hass(), entry_with(
        {'garage_guard_enabled': False, 'garage_door_entity_id': 'cover.door'})
    run(hass, entry)
    assert 'garage_guard_enabled' not in entry.options
    assert 'garage_door_entity_id' not in entry.options


def test_an_entry_that_never_had_the_keys_is_not_rewritten():
    hass, entry = Hass(), entry_with({'garage_bindings': {'duid': {'enabled': True}}})
    run(hass, entry)
    assert hass.updated == []


# --- migration keeps the door -------------------------------------------------

def test_an_enabled_pair_that_never_found_a_binding_migrates_to_a_disabled_one():
    hass, entry = Hass(), entry_with(
        {'garage_guard_enabled': True, 'garage_door_entity_id': 'cover.door'})
    run(hass, entry)
    assert 'garage_guard_enabled' not in entry.options
    assert 'garage_bindings' not in entry.options, (
        'with no robot binding to attach to there is nothing to write')


def test_an_existing_disabled_binding_that_lacks_a_cover_names_the_door():
    hass, entry = Hass(), entry_with({
        'garage_guard_enabled': True,
        'garage_door_entity_id': 'cover.door',
        'garage_bindings': {'duid': {'enabled': False}},
    })
    run(hass, entry)
    binding = entry.options['garage_bindings']['duid']
    assert binding['cover_entity_id'] == 'cover.door'
    assert binding['enabled'] is False, (
        'the produced binding must stay disabled: the old pair carried no '
        'evidence sources, timings or handover acknowledgement')


def test_an_enabled_binding_is_never_overwritten():
    original = {'enabled': True, 'cover_entity_id': 'cover.real'}
    hass, entry = Hass(), entry_with({
        'garage_guard_enabled': True,
        'garage_door_entity_id': 'cover.legacy',
        'garage_bindings': {'duid': dict(original)},
    })
    run(hass, entry)
    assert entry.options['garage_bindings']['duid'] == original


def test_a_binding_with_a_different_cover_is_left_alone():
    original = {'enabled': False, 'cover_entity_id': 'cover.other'}
    hass, entry = Hass(), entry_with({
        'garage_guard_enabled': True,
        'garage_door_entity_id': 'cover.door',
        'garage_bindings': {'duid': dict(original)},
    })
    run(hass, entry)
    assert entry.options['garage_bindings']['duid'] == original, (
        'a later, more specific choice of door must not be replaced by a stale '
        'global option')


def test_a_disabled_pair_migrates_nothing():
    hass, entry = Hass(), entry_with({
        'garage_guard_enabled': False,
        'garage_door_entity_id': 'cover.door',
        'garage_bindings': {'duid': {'enabled': False}},
    })
    run(hass, entry)
    assert 'cover_entity_id' not in entry.options['garage_bindings']['duid']


def test_other_options_are_preserved():
    hass, entry = Hass(), entry_with({
        'garage_guard_enabled': True,
        'garage_door_entity_id': 'cover.door',
        'v1_local_status_poll_interval': 3,
        'drawables': {'path': True},
    })
    run(hass, entry)
    assert entry.options['v1_local_status_poll_interval'] == 3
    assert entry.options['drawables'] == {'path': True}
