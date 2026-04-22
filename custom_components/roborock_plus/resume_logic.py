"""Resume command selection for Roborock Plus."""

APP_CHARGE_COMMAND = "app_charge"
APP_RESUME_BUILD_MAP_COMMAND = "app_resume_build_map"
APP_RESUME_COMMAND = "app_resume"
APP_START_COMMAND = "app_start"
RESUME_SEGMENT_CLEAN_COMMAND = "resume_segment_clean"
RESUME_ZONED_CLEAN_COMMAND = "resume_zoned_clean"


def select_resume_command(
    *,
    in_returning: int | None,
    in_cleaning: int | None,
) -> str | None:
    """Return the appropriate resume command for an interrupted task."""
    if in_returning == 1:
        return APP_CHARGE_COMMAND
    if in_cleaning == 1:
        return APP_RESUME_COMMAND
    if in_cleaning == 2:
        return RESUME_ZONED_CLEAN_COMMAND
    if in_cleaning == 3:
        return RESUME_SEGMENT_CLEAN_COMMAND
    if in_cleaning == 4:
        return APP_RESUME_BUILD_MAP_COMMAND
    return None


def select_start_or_resume_command(
    *,
    in_returning: int | None,
    in_cleaning: int | None,
) -> str:
    """Return the best command for Home Assistant's start semantics."""
    return (
        select_resume_command(
            in_returning=in_returning,
            in_cleaning=in_cleaning,
        )
        or APP_START_COMMAND
    )
