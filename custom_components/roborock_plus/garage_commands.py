"""Explicit nonmoving V1 commands from python-roborock 5.5.1.

Adding a firmware command is a classification decision, never a prefix rule.
Map loading/recovery, timer programming, direct actuator controls and generic
setting/command containers stay outside this list because they can create or
alter movement without going through the garage task controller.
"""

from __future__ import annotations


READ_ONLY_COMMANDS = frozenset({
    "app_get_amethyst_status", "app_get_carpet_deep_clean_status",
    "app_get_clean_estimate_info", "app_get_dryer_setting", "app_get_init_status",
    "app_get_locale", "app_get_robot_setting", "app_get_wifi_list",
    "get_auto_delivery_cleaning_fluid", "get_camera_status", "get_carpet_clean_mode",
    "get_carpet_mode", "get_child_lock_status", "get_clean_follow_ground_material_status",
    "get_clean_motor_mode", "get_clean_record", "get_clean_record_map", "get_clean_sequence",
    "get_clean_summary", "get_collision_avoid_status", "get_consumable", "get_current_sound",
    "get_custom_mode", "get_customize_clean_mode", "get_dnd_timer", "get_dock_info",
    "get_dust_collection_mode", "get_dust_collection_switch_status", "get_dynamic_data",
    "get_dynamic_map_diff", "get_fan_motor_work_timeout", "get_flow_led_status", "get_fresh_map",
    "get_fw_features", "get_homesec_connect_status", "get_identify_furniture_status",
    "get_identify_ground_material_status", "get_led_status", "get_log_upload_status",
    "get_map", "get_map_beautification_status", "get_map_status", "get_map_v1", "get_map_v2",
    "get_map_calibration", "get_mop_motor_status", "get_mop_template_params_by_id",
    "get_mop_template_params_summary", "get_multi_map", "get_multi_maps_list", "get_network_info",
    "get_offline_map_status", "get_persist_map", "get_prop", "get_recover_map", "get_recover_maps",
    "get_room_mapping", "get_scenes_valid_tids", "get_segment_status", "get_serial_number",
    "get_server_timer", "get_smart_wash_params", "get_sound_progress", "get_sound_volume",
    "get_status", "get_testid", "get_timer", "get_timer_detail", "get_timer_summary", "get_timezone",
    "get_valley_electricity_timer", "get_wash_debug_params", "get_wash_towel_mode",
    "get_wash_towel_params", "get_water_box_custom_mode", "get_stretch_tag_status",
    "get_right_brush_stretch_status", "get_dirty_object_detect_status", "get_wash_water_temperature",
    "get_pet_supplies_deep_clean_status", "get_ap_mic_led_status", "get_handle_leak_water_status",
    "get_gap_deep_clean_status", "matter.get_status",
})

SETTING_COMMANDS = frozenset({
    "change_sound_volume", "close_dnd_timer", "close_valley_electricity_timer", "reset_consumable",
    "set_airdry_hours", "set_app_timezone", "set_auto_delivery_cleaning_fluid", "set_carpet_clean_mode",
    "set_carpet_mode", "set_child_lock_status", "set_clean_follow_ground_material_status",
    "set_clean_motor_mode", "set_clean_sequence", "set_clean_repeat_times", "set_collision_avoid_status",
    "set_custom_mode", "set_customize_clean_mode", "set_dnd_timer", "set_dnd_timer_actions",
    "set_dust_collection_mode", "set_dust_collection_switch_status", "set_fan_motor_work_timeout",
    "set_flow_led_status", "set_identify_furniture_status", "set_identify_ground_material_status",
    "set_led_status", "set_map_beautification_status", "set_mop_mode", "set_smart_wash_params",
    "set_timezone", "set_valley_electricity_timer", "set_voice_chat_volume", "set_wash_towel_mode",
    "set_wash_towel_params", "set_water_box_custom_mode", "set_water_box_distance_off",
    "set_wash_water_temperature", "set_ap_mic_led_status",
})

# Preserve explicit pause/stop and the existing audible locate entry. Unlike a
# resume, these commands cannot start a new robot passage through the door.
SAFE_DIRECT_COMMANDS = READ_ONLY_COMMANDS | SETTING_COMMANDS | frozenset({
    "app_pause", "app_stop", "find_me",
})

# Commands that may move the robot out of its dock, so a clean they trigger has
# to go through the garage controller. Moved here from the retired flat guard:
# the classification belongs with the other command lists, not with the door
# handling that used to consume it.
GUARDED_CLEAN_COMMANDS = frozenset({
    "APP_GOTO_TARGET",
    "APP_START",
    "APP_SEGMENT_CLEAN",
    "APP_SPOT",
    "APP_ZONED_CLEAN",
    "app_goto_target",
    "app_start",
    "app_segment_clean",
    "app_spot",
    "app_zoned_clean",
})


def command_value(command: object) -> str:
    """Return the Roborock wire command value for an enum or a plain string."""
    value = getattr(command, "value", None)
    return str(value) if value is not None else str(command)


def should_guard_clean_command(command: object) -> bool:
    """Return whether a command may move the robot out of its dock."""
    return command_value(command) in GUARDED_CLEAN_COMMANDS
