"""Tests for V1 map-position sampling helpers.

Map content always travels to the configured server (the upstream trait is
marked `map_rpc_channel` and has no local fallback), so these tests pin the
rate-limit safety behaviour: official servers must stay slow, and an override
must not be able to speed them up past the hard floor.
"""

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

CONF_V1_MAP_POSITION_POLL_INTERVAL = MODULE.CONF_V1_MAP_POSITION_POLL_INTERVAL
MAP_POSITION_OFFICIAL_ACTIVE_INTERVAL = MODULE.MAP_POSITION_OFFICIAL_ACTIVE_INTERVAL
MAP_POSITION_OFFICIAL_IDLE_INTERVAL = MODULE.MAP_POSITION_OFFICIAL_IDLE_INTERVAL
MAP_POSITION_OFFICIAL_MIN_INTERVAL = MODULE.MAP_POSITION_OFFICIAL_MIN_INTERVAL
MAP_POSITION_LOCAL_ACTIVE_INTERVAL = MODULE.MAP_POSITION_LOCAL_ACTIVE_INTERVAL
MAP_POSITION_LOCAL_IDLE_INTERVAL = MODULE.MAP_POSITION_LOCAL_IDLE_INTERVAL
MAX_V1_MAP_POSITION_POLL_INTERVAL = MODULE.MAX_V1_MAP_POSITION_POLL_INTERVAL
get_map_position_intervals = MODULE.get_map_position_intervals
get_map_position_max_age = MODULE.get_map_position_max_age
resolve_map_position_intervals = MODULE.resolve_map_position_intervals
should_refresh_v1_map_position = MODULE.should_refresh_v1_map_position
is_v1_map_position_fresh = MODULE.is_v1_map_position_fresh

NOW = datetime(2026, 4, 27, 18, 0, 0)


def _should_refresh(**overrides) -> bool:
    kwargs = {
        "now": NOW,
        "last_attempt": None,
        "task_active": True,
        "is_official_cloud": True,
    }
    kwargs.update(overrides)
    return should_refresh_v1_map_position(**kwargs)


# --- server-derived cadence -------------------------------------------------


def test_official_cloud_is_polled_conservatively() -> None:
    active, idle = get_map_position_intervals(is_official_cloud=True)

    assert active == MAP_POSITION_OFFICIAL_ACTIVE_INTERVAL
    assert idle == MAP_POSITION_OFFICIAL_IDLE_INTERVAL
    # Must not be fast enough to risk the official rate limits.
    assert active >= timedelta(seconds=30)
    assert idle >= timedelta(minutes=5)


def test_self_hosted_server_is_polled_fast() -> None:
    active, idle = get_map_position_intervals(is_official_cloud=False)

    assert active == MAP_POSITION_LOCAL_ACTIVE_INTERVAL
    assert idle == MAP_POSITION_LOCAL_IDLE_INTERVAL


def test_self_hosted_is_much_faster_than_official() -> None:
    official_active, _ = get_map_position_intervals(is_official_cloud=True)
    local_active, _ = get_map_position_intervals(is_official_cloud=False)

    assert local_active * 4 <= official_active


# --- automatic fallback (no option set) -------------------------------------


def test_unset_option_falls_back_to_the_server_default() -> None:
    assert resolve_map_position_intervals(
        {}, is_official_cloud=True
    ) == get_map_position_intervals(is_official_cloud=True)
    assert resolve_map_position_intervals(
        {}, is_official_cloud=False
    ) == get_map_position_intervals(is_official_cloud=False)


def test_zero_option_means_auto() -> None:
    assert resolve_map_position_intervals(
        {CONF_V1_MAP_POSITION_POLL_INTERVAL: 0}, is_official_cloud=False
    ) == get_map_position_intervals(is_official_cloud=False)


def test_garbage_option_falls_back_to_the_server_default() -> None:
    for bad in ("10", None, 1.5, True, [10]):
        assert resolve_map_position_intervals(
            {CONF_V1_MAP_POSITION_POLL_INTERVAL: bad}, is_official_cloud=True
        ) == get_map_position_intervals(is_official_cloud=True), bad


# --- explicit override (feature A) ------------------------------------------


def test_override_applies_on_a_self_hosted_server() -> None:
    active, idle = resolve_map_position_intervals(
        {CONF_V1_MAP_POSITION_POLL_INTERVAL: 20}, is_official_cloud=False
    )

    assert active == timedelta(seconds=20)
    assert idle >= active


def test_override_can_slow_an_official_server_down() -> None:
    active, _ = resolve_map_position_intervals(
        {CONF_V1_MAP_POSITION_POLL_INTERVAL: 120}, is_official_cloud=True
    )

    assert active == timedelta(seconds=120)


def test_override_cannot_speed_official_servers_past_the_floor() -> None:
    """The safety net for feature A: a small value must not get us banned."""
    for requested in (1, 2, 5, 10, 29):
        active, _ = resolve_map_position_intervals(
            {CONF_V1_MAP_POSITION_POLL_INTERVAL: requested},
            is_official_cloud=True,
        )
        assert active >= MAP_POSITION_OFFICIAL_MIN_INTERVAL, requested


def test_override_is_capped_on_an_absurd_value() -> None:
    active, _ = resolve_map_position_intervals(
        {CONF_V1_MAP_POSITION_POLL_INTERVAL: 999_999},
        is_official_cloud=True,
    )

    assert active == timedelta(seconds=MAX_V1_MAP_POSITION_POLL_INTERVAL)


# --- scheduling -------------------------------------------------------------


def test_refreshes_on_first_sample() -> None:
    assert _should_refresh(last_attempt=None)


def test_official_server_waits_a_full_minute_while_cleaning() -> None:
    just_under = MAP_POSITION_OFFICIAL_ACTIVE_INTERVAL - timedelta(seconds=1)

    assert not _should_refresh(last_attempt=NOW - just_under)
    assert _should_refresh(last_attempt=NOW - MAP_POSITION_OFFICIAL_ACTIVE_INTERVAL)


def test_idle_refreshes_more_slowly_than_active() -> None:
    active, idle = get_map_position_intervals(is_official_cloud=True)
    gap = active + timedelta(seconds=1)

    assert gap < idle
    assert _should_refresh(last_attempt=NOW - gap, task_active=True)
    assert not _should_refresh(last_attempt=NOW - gap, task_active=False)
    assert _should_refresh(last_attempt=NOW - idle, task_active=False)


def test_self_hosted_server_refreshes_quickly_while_cleaning() -> None:
    """The whole point of the local server: the position keeps up."""
    assert _should_refresh(
        last_attempt=NOW - MAP_POSITION_LOCAL_ACTIVE_INTERVAL,
        is_official_cloud=False,
    )


# --- freshness / fail-safe --------------------------------------------------


def test_max_age_follows_the_active_interval() -> None:
    """A short interval must not leave the entity flapping to unknown."""
    official = get_map_position_max_age(is_official_cloud=True)
    local = get_map_position_max_age(is_official_cloud=False)

    assert official > MAP_POSITION_OFFICIAL_ACTIVE_INTERVAL
    assert local > MAP_POSITION_LOCAL_ACTIVE_INTERVAL
    assert official > local


def test_max_age_does_not_survive_two_consecutive_failed_reads() -> None:
    """Fail safe: a stale 'clear of garage' must not outlive missed samples.

    Allowing a sample to be trusted for three intervals would let the robot
    drive back into the zone while the reading still said it was clear.
    """
    for is_official in (True, False):
        active, _ = get_map_position_intervals(is_official_cloud=is_official)
        max_age = get_map_position_max_age(
            is_official_cloud=is_official, active_interval=active
        )
        missed_before_expiry = (max_age.total_seconds() - 1) // active.total_seconds()

        assert missed_before_expiry <= 1, (is_official, max_age, active)


def test_self_hosted_cadence_fits_the_garage_door_wait_window() -> None:
    """The departing automation only waits two minutes for the robot to clear.

    Against a self-hosted server the position must be able to go fresh well
    inside that window; against the official cloud it is inherently tight,
    which is the documented reason to self-host.
    """
    wait_window = timedelta(seconds=120)

    local_active, _ = get_map_position_intervals(is_official_cloud=False)
    assert local_active * 2 <= wait_window

    official_active, _ = get_map_position_intervals(is_official_cloud=True)
    assert official_active < wait_window
    # Tighter than the local case, and worth calling out.
    assert official_active > local_active


def test_position_is_fresh_within_max_age() -> None:
    max_age = get_map_position_max_age(is_official_cloud=False)

    assert is_v1_map_position_fresh(
        now=NOW, position_time=NOW, task_active=True, is_official_cloud=False
    )
    assert is_v1_map_position_fresh(
        now=NOW,
        position_time=NOW - max_age,
        task_active=True,
        is_official_cloud=False,
    )


def test_position_is_stale_past_max_age_while_task_active() -> None:
    max_age = get_map_position_max_age(is_official_cloud=False)

    assert not is_v1_map_position_fresh(
        now=NOW,
        position_time=NOW - max_age - timedelta(seconds=1),
        task_active=True,
        is_official_cloud=False,
    )


def test_unsampled_position_is_not_trusted_while_task_active() -> None:
    """Fail safe: an unsampled position must not be trusted mid-task."""
    assert not is_v1_map_position_fresh(
        now=NOW, position_time=None, task_active=True
    )


def test_idle_trusts_last_known_position() -> None:
    """A parked robot cannot move, so an old sample is still the best answer."""
    assert is_v1_map_position_fresh(
        now=NOW, position_time=NOW - timedelta(days=1), task_active=False
    )
    assert is_v1_map_position_fresh(now=NOW, position_time=None, task_active=False)
