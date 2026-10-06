"""Tests for V1 map-position freshness helpers."""

from datetime import datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "v1_map_position.py"
)
SPEC = spec_from_file_location("roborock_plus_v1_map_position", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

MAP_POSITION_ACTIVE_INTERVAL = MODULE.MAP_POSITION_ACTIVE_INTERVAL
MAP_POSITION_IDLE_INTERVAL = MODULE.MAP_POSITION_IDLE_INTERVAL
MAP_POSITION_MAX_AGE = MODULE.MAP_POSITION_MAX_AGE
is_v1_map_position_fresh = MODULE.is_v1_map_position_fresh
should_refresh_v1_map_position = MODULE.should_refresh_v1_map_position

NOW = datetime(2026, 4, 27, 18, 0, 0)


def _should_refresh(**overrides) -> bool:
    kwargs = {
        "now": NOW,
        "last_attempt": None,
        "task_active": True,
        "is_local_connected": True,
    }
    kwargs.update(overrides)
    return should_refresh_v1_map_position(**kwargs)


def test_refreshes_on_first_sample() -> None:
    assert _should_refresh(last_attempt=None)


def test_active_task_refreshes_on_the_active_interval() -> None:
    active = MAP_POSITION_ACTIVE_INTERVAL

    assert not _should_refresh(last_attempt=NOW - active + timedelta(seconds=1))
    assert _should_refresh(last_attempt=NOW - active)


def test_idle_refreshes_more_slowly_than_active() -> None:
    idle = MAP_POSITION_IDLE_INTERVAL
    # A gap that would trigger a refresh while active must not while idle.
    gap = MAP_POSITION_ACTIVE_INTERVAL + timedelta(seconds=1)

    assert gap < idle
    assert _should_refresh(last_attempt=NOW - gap, task_active=True)
    assert not _should_refresh(last_attempt=NOW - gap, task_active=False)
    assert _should_refresh(last_attempt=NOW - idle, task_active=False)


def test_never_refreshes_over_the_cloud() -> None:
    """Cloud polling already refreshes maps; do not add rate-limit pressure."""
    assert not _should_refresh(is_local_connected=False, last_attempt=None)
    assert not _should_refresh(
        is_local_connected=False,
        last_attempt=NOW - timedelta(hours=1),
    )


def test_active_interval_is_well_inside_the_automation_wait_window() -> None:
    """The departing automation only waits two minutes for the robot to clear."""
    assert MAP_POSITION_ACTIVE_INTERVAL <= timedelta(seconds=30)
    assert MAP_POSITION_ACTIVE_INTERVAL < MAP_POSITION_MAX_AGE


def test_position_is_fresh_within_max_age() -> None:
    assert is_v1_map_position_fresh(now=NOW, position_time=NOW, task_active=True)
    assert is_v1_map_position_fresh(
        now=NOW,
        position_time=NOW - MAP_POSITION_MAX_AGE,
        task_active=True,
    )


def test_position_is_stale_past_max_age_while_task_active() -> None:
    assert not is_v1_map_position_fresh(
        now=NOW,
        position_time=NOW - MAP_POSITION_MAX_AGE - timedelta(seconds=1),
        task_active=True,
    )


def test_unsampled_position_is_not_trusted_while_task_active() -> None:
    """Fail safe: an unsampled position must not be trusted mid-task."""
    assert not is_v1_map_position_fresh(
        now=NOW,
        position_time=None,
        task_active=True,
    )


def test_idle_trusts_last_known_position() -> None:
    """A parked robot cannot move, so an old sample is still the best answer.

    This keeps cloud-only setups, which never run the fast position sampler,
    from reporting `unknown` forever.
    """
    assert is_v1_map_position_fresh(
        now=NOW,
        position_time=NOW - timedelta(days=1),
        task_active=False,
    )
    assert is_v1_map_position_fresh(now=NOW, position_time=None, task_active=False)
