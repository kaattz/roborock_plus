"""Resume command selection for Roborock Plus."""

APP_RESUME_BUILD_MAP_COMMAND = "app_resume_build_map"
APP_RESUME_COMMAND = "app_resume"
APP_START_COMMAND = "app_start"

_ACTIVE_STATE_RESUME_COMMANDS = {
    "cleaning": APP_RESUME_COMMAND,
    "docking": APP_RESUME_COMMAND,
    "going_to_wash_the_mop": APP_RESUME_COMMAND,
    "manual_mode": APP_RESUME_COMMAND,
    "remote_control_active": APP_RESUME_COMMAND,
    "returning_home": APP_RESUME_COMMAND,
    "spot_cleaning": APP_RESUME_COMMAND,
    "starting": APP_RESUME_COMMAND,
    "zoned_cleaning": APP_RESUME_COMMAND,
    "segment_cleaning": APP_RESUME_COMMAND,
}

_CLEAN_COMMAND_RESUME_COMMANDS = {
    "APP_START": APP_RESUME_COMMAND,
    "APP_SEGMENT_CLEAN": APP_RESUME_COMMAND,
    "APP_ZONED_CLEAN": APP_RESUME_COMMAND,
    "app_start": APP_RESUME_COMMAND,
    "app_segment_clean": APP_RESUME_COMMAND,
    "app_zoned_clean": APP_RESUME_COMMAND,
}


def select_resume_command(
    *,
    in_returning: int | None,
    in_cleaning: int | None,
    state: object | None = None,
    fallback_command: str | None = None,
) -> str | None:
    """Return the appropriate resume command for an interrupted task."""
    if in_returning == 1:
        return APP_RESUME_COMMAND
    if in_cleaning == 1:
        return APP_RESUME_COMMAND
    if in_cleaning == 2:
        return APP_RESUME_COMMAND
    if in_cleaning == 3:
        return APP_RESUME_COMMAND
    if in_cleaning == 4:
        return APP_RESUME_BUILD_MAP_COMMAND
    if _state_name(state) == "paused":
        return fallback_command
    return None


def select_resume_command_from_status(
    *,
    state: object | None,
    in_returning: int | None,
    in_cleaning: int | None,
) -> str | None:
    """Return the command that should resume the current active status."""
    return select_resume_command(
        in_returning=in_returning,
        in_cleaning=in_cleaning,
    ) or _ACTIVE_STATE_RESUME_COMMANDS.get(_state_name(state))


def select_resume_command_for_clean_command(command: object) -> str | None:
    """Return the resume command that matches a start-like clean command."""
    return _CLEAN_COMMAND_RESUME_COMMANDS.get(_command_value(command))


def select_start_or_resume_command(
    *,
    in_returning: int | None,
    in_cleaning: int | None,
    state: object | None = None,
    fallback_command: str | None = None,
) -> str:
    """Return the best command for Home Assistant's start semantics."""
    return (
        select_resume_command(
            in_returning=in_returning,
            in_cleaning=in_cleaning,
            state=state,
            fallback_command=fallback_command,
        )
        or APP_START_COMMAND
    )


def _command_value(command: object) -> str:
    if hasattr(command, "value"):
        return str(command.value)
    if hasattr(command, "name"):
        return str(command.name)
    return str(command)


def _state_name(state: object | None) -> str:
    if state is None:
        return ""
    return str(getattr(state, "name", state))
