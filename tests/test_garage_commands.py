"""Known nonmoving commands keep their transport contract under managed mode.

The classification in ``garage_commands.py`` decides whether a command may reach
the robot without going through the garage sequence. A command wrongly classed as
"safe direct" could move the robot; one wrongly classed as guarded would break an
ordinary setting. The list is pinned here, and the passthrough is checked against
the real runtime.

The old version of this file built its fixtures on the task-session object that no
longer exists; the classification assertions are unchanged, only the harness is.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace
from _garage_pkg.garage_door import GarageControlError
from _garage_pkg.garage_runtime import GarageRuntime

ROOT = Path(__file__).parents[1] / "custom_components/roborock_plus"


def commands_module():
    assert (ROOT / "garage_commands.py").is_file(), "known safe command classification is missing"
    package = ModuleType("_garage_commands_tests")
    package.__path__ = [str(ROOT)]
    sys.modules.setdefault(package.__name__, package)
    return importlib.import_module(f"{package.__name__}.garage_commands")


async def _ok():
    return None


def _binding():
    return {
        'enabled': True,
        'cover_entity_id': 'cover.door',
        'task_door_mode': 'after_exit',
        'open_source': 'contact',
        'close_source': 'contact',
        'open_contact': 'binary_sensor.open',
        'close_contact': 'binary_sensor.closed',
        'open_contact_state': 'on',
        'close_contact_state': 'on',
        'contact_settle_seconds': 0,
        'handover_confirmed': True,
        'operation_timeout_seconds': 5,
    }


def _coordinator():
    return NS(
        duid='robot', duid_slug='robot', last_update_success=True,
        update_interval=dt.timedelta(seconds=1),
        garage_status_sample={
            'sampled_at': dt.datetime.fromtimestamp(1000.0, dt.timezone.utc),
            'state': 'charging', 'in_cleaning': 0, 'in_returning': 0,
        },
        last_map_position_update=dt.datetime.fromtimestamp(1000.0, dt.timezone.utc),
        garage_vacuum_entity_id='vacuum.robot',
        last_resume_command=None,
        config_entry=NS(options={}, entry_id='entry'),
        data=NS(clean_summary=None),
        properties_api=NS(
            map_content=NS(map_data=NS(charger=NS(x=0.0, y=0.0))),
            command=NS(send=lambda name, params=None: _ok()),
        ),
        resolve_vacuum_position=lambda: NS(x=0.0, y=0.0, trusted=True),
        async_refresh=_ok,
    )


def _runtime():
    return GarageRuntime(
        NS(states=NS(get=lambda _: None), services=NS(), bus=NS(), data={}),
        _coordinator(), _binding(), store=None, zone_provider=lambda: None,
    )


@pytest.mark.parametrize("command", [
    "get_sound_volume", "change_sound_volume", "get_dnd_timer", "set_dnd_timer",
    "get_child_lock_status", "set_child_lock_status", "get_flow_led_status", "set_flow_led_status",
    "get_wash_towel_mode", "set_wash_towel_mode", "get_water_box_custom_mode",
    "set_water_box_custom_mode", "get_custom_mode", "set_custom_mode", "reset_consumable",
    "get_map_v1", "get_room_mapping", "get_clean_summary", "get_timezone", "set_timezone",
])
def test_known_reads_and_settings_are_classified_as_direct(command):
    """These cannot move the robot, so they must not go through the door sequence."""
    module = commands_module()
    assert command in module.SAFE_DIRECT_COMMANDS, (
        f'{command} would be forced through the garage sequence, breaking an '
        f'ordinary setting or read')


@pytest.mark.parametrize("command", [
    "app_start", "app_segment_clean", "app_zoned_clean", "app_spot",
    "app_goto_target", "app_resume", "app_resume_build_map", "app_charge",
])
def test_movement_commands_are_never_classified_as_direct(command):
    """Anything that can move the robot must not bypass the door sequence."""
    module = commands_module()
    assert command not in module.SAFE_DIRECT_COMMANDS, (
        f'{command} can move the robot out of the cabinet; it must be guarded')


def test_pause_and_stop_stay_direct_so_a_person_can_always_intervene():
    """A stop that had to wait for a door decision would be useless in an emergency."""
    module = commands_module()
    assert "app_pause" in module.SAFE_DIRECT_COMMANDS
    assert "app_stop" in module.SAFE_DIRECT_COMMANDS


def test_clean_commands_are_guarded():
    module = commands_module()
    for command in ("app_start", "app_segment_clean", "app_zoned_clean", "app_spot"):
        assert module.should_guard_clean_command(command) is True


def test_a_direct_command_reaches_the_transport_untouched():
    """The runtime must relay a safe command without touching the door."""
    sent = []

    async def transport():
        sent.append('device')
        return {'result': ['original-device-response']}

    result = asyncio.run(_runtime().async_send('get_sound_volume', transport))
    assert result == {'result': ['original-device-response']}
    assert sent == ['device']


@pytest.mark.parametrize("command", [
    "app_start", "app_segment_clean", "app_zoned_clean", "app_spot", "app_goto_target",
    "app_resume",
])
def test_a_clean_command_never_reaches_the_device_without_a_door(command):
    """With no usable door evidence a clean must be refused, not relayed.

    ``app_charge`` is deliberately absent: sending the robot home does not need
    the door open, and refusing it would strand the robot outside.
    """
    device = []

    async def transport():
        device.append('device')
        return {'ok': True}

    try:
        asyncio.run(_runtime().async_send(command, transport))
    except GarageControlError:
        pass
    assert device == [], f'{command} bypassed the garage sequence'


def test_app_charge_is_relayed_without_touching_the_door():
    """Driving home is always allowed: the door is only needed on the way back in,
    which is handled by the return rule, not by blocking the command."""
    device = []

    async def transport():
        device.append('device')
        return {'ok': True}

    asyncio.run(_runtime().async_send('app_charge', transport))
    assert device == ['device']
