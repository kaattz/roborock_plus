"""Helpers for V1 status-only polling."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

CONF_V1_LOCAL_STATUS_POLL_INTERVAL = "v1_local_status_poll_interval"
DEFAULT_V1_LOCAL_STATUS_POLL_INTERVAL = 5
MIN_V1_LOCAL_STATUS_POLL_INTERVAL = 1
MAX_V1_LOCAL_STATUS_POLL_INTERVAL = 60


def get_v1_local_status_poll_interval(options: Mapping[str, Any]) -> timedelta:
    """Return the configured local status-only poll interval."""
    raw_interval = options.get(
        CONF_V1_LOCAL_STATUS_POLL_INTERVAL,
        DEFAULT_V1_LOCAL_STATUS_POLL_INTERVAL,
    )
    if not isinstance(raw_interval, int):
        raise ValueError("V1 local status poll interval must be an integer")
    if not (
        MIN_V1_LOCAL_STATUS_POLL_INTERVAL
        <= raw_interval
        <= MAX_V1_LOCAL_STATUS_POLL_INTERVAL
    ):
        raise ValueError(
            "V1 local status poll interval must be between "
            f"{MIN_V1_LOCAL_STATUS_POLL_INTERVAL} and "
            f"{MAX_V1_LOCAL_STATUS_POLL_INTERVAL} seconds"
        )
    return timedelta(seconds=raw_interval)


def should_refresh_full_v1_data(
    *,
    now: datetime,
    last_full_update: datetime | None,
    full_update_interval: timedelta,
) -> bool:
    """Return whether the slower full V1 refresh should run now."""
    return last_full_update is None or now - last_full_update >= full_update_interval
