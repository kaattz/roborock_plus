from datetime import datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "v1_status_polling.py"
)
SPEC = spec_from_file_location("roborock_plus_v1_status_polling", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CONF_V1_LOCAL_STATUS_POLL_INTERVAL = MODULE.CONF_V1_LOCAL_STATUS_POLL_INTERVAL
get_v1_local_status_poll_interval = MODULE.get_v1_local_status_poll_interval
should_refresh_full_v1_data = MODULE.should_refresh_full_v1_data


def test_get_v1_local_status_poll_interval_uses_default_for_missing_option() -> None:
    assert get_v1_local_status_poll_interval({}) == timedelta(seconds=5)


def test_get_v1_local_status_poll_interval_uses_configured_seconds() -> None:
    assert get_v1_local_status_poll_interval(
        {CONF_V1_LOCAL_STATUS_POLL_INTERVAL: 1}
    ) == timedelta(seconds=1)


@pytest.mark.parametrize("value", [0, 61])
def test_get_v1_local_status_poll_interval_rejects_out_of_range_values(
    value: int,
) -> None:
    with pytest.raises(ValueError):
        get_v1_local_status_poll_interval(
            {CONF_V1_LOCAL_STATUS_POLL_INTERVAL: value}
        )


def test_should_refresh_full_v1_data_requires_full_refresh_first() -> None:
    assert should_refresh_full_v1_data(
        now=datetime(2026, 4, 25, 18, 0, 0),
        last_full_update=None,
        full_update_interval=timedelta(seconds=30),
    )


def test_should_refresh_full_v1_data_waits_for_full_refresh_interval() -> None:
    now = datetime(2026, 4, 25, 18, 0, 30)

    assert not should_refresh_full_v1_data(
        now=now,
        last_full_update=now - timedelta(seconds=29),
        full_update_interval=timedelta(seconds=30),
    )
    assert should_refresh_full_v1_data(
        now=now,
        last_full_update=now - timedelta(seconds=30),
        full_update_interval=timedelta(seconds=30),
    )
