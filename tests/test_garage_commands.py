"""Known nonmoving commands retain their transport contract in managed mode."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType

import pytest

from test_garage_runtime import Boundaries


ROOT = Path(__file__).parents[1] / "custom_components/roborock_plus"


def commands_module():
    assert (ROOT / "garage_commands.py").is_file(), "known safe command classification is missing"
    package = ModuleType("_garage_commands_tests")
    package.__path__ = [str(ROOT)]
    sys.modules.setdefault(package.__name__, package)
    return importlib.import_module(f"{package.__name__}.garage_commands")


@pytest.mark.parametrize("command", [
    "get_sound_volume", "change_sound_volume", "get_dnd_timer", "set_dnd_timer",
    "get_child_lock_status", "set_child_lock_status", "get_flow_led_status", "set_flow_led_status",
    "get_wash_towel_mode", "set_wash_towel_mode", "get_water_box_custom_mode",
    "set_water_box_custom_mode", "get_custom_mode", "set_custom_mode", "reset_consumable",
    "get_map_v1", "get_room_mapping", "get_clean_summary", "get_timezone", "set_timezone",
])
def test_known_reads_and_settings_relay_unchanged_without_door_or_task(command):
    boundary = Boundaries()
    runtime = boundary.create()
    calls = []
    runtime.schedule = lambda: calls.append("schedule")
    before = runtime.session.to_dict()
    response = {"result": ["original-device-response"]}

    async def send():
        calls.append(command)
        return response

    result = asyncio.run(runtime.async_send(command, send))
    assert result is response
    assert calls == [command, "schedule"]
    assert runtime.session.to_dict() == before
    assert boundary.events == []


@pytest.mark.parametrize("command", [
    "get_new_unknown_command", "set_unknown_motor_mode", "app_rc_start", "app_rc_move",
    "load_multi_map", "recover_map", "recover_multi_map", "reset_map", "resolve_error",
    "start_wash_then_charge", "app_start_build_map", "app_start_wash", "app_start_patrol",
    "set_mop_motor_status", "app_set_dryer_status", "app_set_robot_setting",
])
def test_unknown_or_potentially_moving_commands_do_not_use_safe_direct_path(command):
    boundary = Boundaries()
    runtime = boundary.create()
    calls = []
    runtime.schedule = lambda: None

    async def send():
        calls.append(command)

    with pytest.raises(Exception, match="unsupported_guarded_command"):
        asyncio.run(runtime.async_send(command, send))
    assert calls == []


def test_safe_categories_are_explicit_and_disjoint_from_guarded_movement():
    module = commands_module()
    assert module.READ_ONLY_COMMANDS.isdisjoint(module.SETTING_COMMANDS)
    assert {"get_status", "get_sound_volume", "get_dnd_timer"} <= module.READ_ONLY_COMMANDS
    assert {"change_sound_volume", "set_child_lock_status", "set_flow_led_status"} <= module.SETTING_COMMANDS
    assert module.SAFE_DIRECT_COMMANDS.isdisjoint({
        "app_start", "app_charge", "app_resume", "app_goto_target", "app_segment_clean",
        "app_zoned_clean", "app_spot", "resume_segment_clean", "resume_zoned_clean",
    })
