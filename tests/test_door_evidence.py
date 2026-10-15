"""Exercise the real door evidence without importing the HA integration package."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "custom_components" / "roborock_plus" / "door_evidence.py"


@pytest.fixture
def evidence_type():
    assert MODULE_PATH.is_file(), "the door evidence implementation is missing"
    spec = spec_from_file_location("roborock_plus_door_evidence_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DoorEvidence


def binding(**overrides):
    result = {
        "open_source": "contact",
        "close_source": "contact",
        "open_contact": "binary_sensor.door_open",
        "close_contact": "binary_sensor.door_closed",
        "open_contact_state": "on",
        "close_contact_state": "off",
        "position_kind": "measured",
        "open_position": 95,
        "close_position": 5,
        "open_travel_seconds": 10,
        "close_travel_seconds": 20,
        "travel_margin_seconds": 2,
        "contact_settle_seconds": 2,
        "position_settle_seconds": 3,
        "operation_timeout_seconds": 60,
        "allow_estimated": False,
    }
    result.update(overrides)
    return result


def observe(evidence, now, **values):
    return evidence.observe(now, cover_available=True, **values)


def test_contacts_have_independent_polarity_and_settle(evidence_type):
    evidence = evidence_type(binding())
    first = observe(evidence, 0, open_contact="on", close_contact="on")
    assert first.open.reached is None
    assert first.close.reached is False
    state = observe(evidence, 2, open_contact="on", close_contact="on")
    assert state.open_confirmed is True
    assert state.closed_confirmed is False
    assert state.passage_allowed is True
    assert state.open.source == "contact"


def test_two_not_reached_endpoints_are_valid_middle_state(evidence_type):
    state = observe(evidence_type(binding()), 0, open_contact="off", close_contact="on")
    assert state.open_confirmed is False
    assert state.closed_confirmed is False
    assert state.passage_allowed is False


def test_contact_is_not_replaced_by_position_when_not_reached(evidence_type):
    state = observe(
        evidence_type(binding()), 100, position=100, open_contact="off", close_contact="on"
    )
    assert state.open.reached is False
    assert state.open.source == "contact"
    assert state.passage_allowed is False


@pytest.mark.parametrize("invalid", [None, "unknown", "unavailable", "invalid", True, 1])
def test_bad_contact_is_unknown_without_position_fallback(evidence_type, invalid):
    evidence = evidence_type(binding())
    observe(evidence, 0, open_contact="on", close_contact="on")
    assert observe(evidence, 2, open_contact="on", close_contact="on").passage_allowed
    state = observe(evidence, 4, position=100, open_contact=invalid, close_contact="on")
    assert state.open.reached is None
    assert state.open.quality == "unknown"
    assert state.passage_allowed is None
    assert state.open.reason == "feedback_unavailable"


def test_conflicting_endpoints_revoke_both_before_settle(evidence_type):
    state = observe(evidence_type(binding()), 0, open_contact="on", close_contact="off")
    assert state.open.reason == state.close.reason == "endpoint_conflict"
    assert state.open_confirmed is state.closed_confirmed is None
    assert state.passage_allowed is None


def test_unselected_old_contacts_do_not_change_position_source(evidence_type):
    evidence = evidence_type(binding(open_source="position", close_source="position"))
    observe(evidence, 0, position=100, open_contact="on", close_contact="off")
    state = observe(evidence, 3, position=100, open_contact="on", close_contact="off")
    assert state.open.reason is None
    assert state.passage_allowed is True


def test_conflict_resets_stability_and_timer(evidence_type):
    evidence = evidence_type(binding())
    observe(evidence, 0, open_contact="on", close_contact="on")
    observe(evidence, 2, open_contact="on", close_contact="off")
    assert observe(evidence, 3, open_contact="on", close_contact="on").open_confirmed is None
    assert observe(evidence, 5, open_contact="on", close_contact="on").open_confirmed is True


def test_releasing_endpoint_revokes_without_waiting(evidence_type):
    evidence = evidence_type(binding())
    observe(evidence, 0, open_contact="on", close_contact="on")
    assert observe(evidence, 2, open_contact="on", close_contact="on").open_confirmed
    assert observe(evidence, 2.1, open_contact="off", close_contact="on").open_confirmed is False
    assert observe(evidence, 4, open_contact="on", close_contact="on").open_confirmed is None


def test_measured_position_can_confirm_without_command_or_full_travel(evidence_type):
    evidence = evidence_type(binding(open_source="position", close_source="position"))
    assert observe(evidence, 0, position=96).open_confirmed is None
    state = observe(evidence, 3, position=96)
    assert state.open_confirmed is True
    assert state.closed_confirmed is False
    assert state.passage_allowed is True


@pytest.mark.parametrize("invalid", [None, True, False, "100", float("nan"), float("inf"), -1, 101, 10**1000])
def test_invalid_positions_never_confirm(evidence_type, invalid):
    evidence = evidence_type(binding(open_source="position", close_source="position"))
    observe(evidence, 0, position=100)
    state = observe(evidence, 100, position=invalid)
    assert state.open.quality == "unknown"
    assert state.open_confirmed is None
    assert state.closed_confirmed is None
    assert state.passage_allowed is None


def test_position_threshold_crossing_resets_stability(evidence_type):
    evidence = evidence_type(binding(open_source="position", close_source="position"))
    observe(evidence, 0, position=95)
    assert observe(evidence, 2, position=94).open_confirmed is False
    assert observe(evidence, 3, position=95).open_confirmed is None
    assert observe(evidence, 6, position=95).open_confirmed is True


def test_restored_snapshot_revokes_confirmation_and_estimate(evidence_type):
    evidence = evidence_type(binding(open_source="position", close_source="position"))
    observe(evidence, 0, position=100)
    assert observe(evidence, 3, position=100).open_confirmed
    state = observe(evidence, 10, position=100, restored=True)
    assert state.passage_allowed is None
    assert state.open.reason == "restored_snapshot"
    assert observe(evidence, 11, position=100).open_confirmed is None
    assert observe(evidence, 14, position=100).open_confirmed is True


def test_timed_requires_current_command_and_acceptance(evidence_type):
    evidence = evidence_type(binding(open_source="timed", close_source="timed"))
    assert observe(evidence, 1000).open.reached is None
    evidence.note_command("open", 1000, 7)
    assert observe(evidence, 1011.9).open.reached is None
    state = observe(evidence, 1012)
    assert state.open.reached is True
    assert state.open.quality == "estimated"
    assert state.open_confirmed is None
    assert state.passage_allowed is False
    assert state.direction == "open"
    assert state.operation_id == 7


def test_allow_estimated_is_policy_and_not_confirmation(evidence_type):
    evidence = evidence_type(
        binding(open_source="timed", close_source="timed", allow_estimated=True)
    )
    evidence.note_command("open", 0, 1)
    state = observe(evidence, 12)
    assert state.passage_allowed is True
    assert state.open_confirmed is None
    assert state.closed_confirmed is None


def test_truthy_allow_estimated_does_not_grant_permission(evidence_type):
    evidence = evidence_type(binding(open_source="timed", allow_estimated="true"))
    evidence.note_command("open", 0, 1)
    assert observe(evidence, 12).passage_allowed is False


def test_estimated_position_never_uses_target_echo_as_confirmation(evidence_type):
    evidence = evidence_type(
        binding(open_source="position", close_source="position", position_kind="estimated", allow_estimated=True)
    )
    assert observe(evidence, 0, position=100).open.reached is None
    evidence.note_command("open", 1, 3)
    assert observe(evidence, 1.1, position=100).passage_allowed is None
    assert observe(evidence, 12.9, position=100).passage_allowed is None
    state = observe(evidence, 13, position=100)
    assert state.open.reached is True
    assert state.open.quality == "estimated"
    assert state.open_confirmed is None
    assert state.passage_allowed is True


def test_elapsed_time_cannot_replace_estimated_position_threshold(evidence_type):
    evidence = evidence_type(
        binding(open_source="position", position_kind="estimated", allow_estimated=True)
    )
    evidence.note_command("open", 0, 1)
    assert observe(evidence, 12, position=40).passage_allowed is False


def test_timed_directions_have_independent_full_travel(evidence_type):
    evidence = evidence_type(binding(open_source="timed", close_source="timed", allow_estimated=True))
    evidence.note_command("open", 0, 1)
    assert observe(evidence, 12).passage_allowed is True
    evidence.note_command("close", 13, 2)
    assert observe(evidence, 34.9).close.reached is None
    state = observe(evidence, 35)
    assert state.close.reached is True
    assert state.close.quality == "estimated"
    assert state.closed_confirmed is None
    assert state.passage_allowed is False


def test_close_command_blocks_passage_despite_delayed_contact_release(evidence_type):
    evidence = evidence_type(binding(contact_settle_seconds=0))
    assert observe(evidence, 0, open_contact="on", close_contact="on").passage_allowed
    evidence.note_command("close", 1, 2)
    state = observe(evidence, 1, open_contact="on", close_contact="on")
    assert state.passage_allowed is False


def test_command_change_cannot_carry_stability_across_operations(evidence_type):
    evidence = evidence_type(binding())
    observe(evidence, 0, open_contact="on", close_contact="on")
    evidence.note_command("open", 1.9, 2)
    assert observe(evidence, 2, open_contact="on", close_contact="on").open_confirmed is None
    assert observe(evidence, 4, open_contact="on", close_contact="on").open_confirmed


@pytest.mark.parametrize("reason", ["stopped", "manual_override", "command_failed"])
def test_invalidation_clears_timer(evidence_type, reason):
    evidence = evidence_type(binding(open_source="timed", allow_estimated=True))
    evidence.note_command("open", 0, 1)
    evidence.invalidate(reason)
    assert observe(evidence, 20).open.reached is None
    evidence.note_command("open", 21, 2)
    assert observe(evidence, 32).open.reached is None
    assert observe(evidence, 33).passage_allowed


def test_cover_loss_does_not_restore_old_timer(evidence_type):
    evidence = evidence_type(binding(open_source="timed", allow_estimated=True))
    evidence.note_command("open", 0, 1)
    assert observe(evidence, 12).passage_allowed
    state = evidence.observe(13, cover_available=False)
    assert state.open.reached is None
    assert state.passage_allowed is None
    assert observe(evidence, 50).open.reached is None


def test_estimated_position_feedback_loss_cancels_current_estimate(evidence_type):
    evidence = evidence_type(binding(open_source="position", position_kind="estimated", allow_estimated=True))
    evidence.note_command("open", 0, 1)
    observe(evidence, 0, position=100)
    assert observe(evidence, 12, position=100).passage_allowed
    assert observe(evidence, 13, position=None).open.reached is None
    assert observe(evidence, 30, position=100).open.reached is None


def test_clock_rewind_revokes_stable_observation_and_timer(evidence_type):
    evidence = evidence_type(binding(open_source="timed", allow_estimated=True))
    evidence.note_command("open", 1_700_000_000, 1)
    state = observe(evidence, 100)
    assert state.open.reached is None
    assert state.open.reason == "invalid_clock"
    assert observe(evidence, 1_700_000_100).open.reached is None


@pytest.mark.parametrize("invalid_now", [True, float("nan"), float("inf"), None])
def test_bad_clock_is_unknown(evidence_type, invalid_now):
    evidence = evidence_type(binding(open_source="timed", allow_estimated=True))
    evidence.note_command("open", 0, 1)
    assert observe(evidence, invalid_now).passage_allowed is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"open_source": None},
        {"open_source": "auto"},
        {"open_contact": None},
        {"open_contact_state": "open"},
        {"open_contact_state": None},
        {"contact_settle_seconds": True},
        {"contact_settle_seconds": -1},
        {"open_source": "position", "position_kind": None},
        {"open_source": "position", "open_position": float("nan")},
        {"open_source": "position", "open_position": 5},
        {"open_source": "timed", "open_travel_seconds": None},
        {"open_source": "timed", "open_travel_seconds": True},
        {"open_source": "timed", "travel_margin_seconds": -1},
    ],
)
def test_invalid_required_configuration_is_unknown(evidence_type, overrides):
    evidence = evidence_type(binding(**overrides))
    evidence.note_command("open", 0, 1)
    observe(evidence, 0, position=100, open_contact="on", close_contact="on")
    state = observe(evidence, 50, position=100, open_contact="on", close_contact="on")
    assert state.open.reached is None
    assert state.passage_allowed is None


def test_missing_source_is_unknown_even_with_valid_inputs(evidence_type):
    state = observe(evidence_type({}), 100, position=100, open_contact="on", close_contact="off")
    assert state.open.reached is None
    assert state.close.reached is None
    assert state.passage_allowed is None


def test_binding_is_copied_not_live_mutated(evidence_type):
    config = binding(open_source="timed", allow_estimated=False)
    evidence = evidence_type(config)
    config["allow_estimated"] = True
    evidence.note_command("open", 0, 1)
    assert observe(evidence, 12).passage_allowed is False


def test_open_estimate_cannot_override_confirmed_closed_contact(evidence_type):
    evidence = evidence_type(binding(open_source="timed", allow_estimated=True))
    evidence.note_command("open", 0, 1)
    observe(evidence, 0, close_contact="off")
    state = observe(evidence, 12, close_contact="off")
    assert state.passage_allowed is not True
    assert state.open.reason == "endpoint_conflict"


def test_closed_estimate_cannot_override_confirmed_open_contact(evidence_type):
    evidence = evidence_type(binding(close_source="timed", allow_estimated=True))
    evidence.note_command("close", 0, 1)
    observe(evidence, 0, open_contact="on")
    state = observe(evidence, 22, open_contact="on")
    assert state.close.reached is None
    assert state.close.reason == "endpoint_conflict"
    assert state.passage_allowed is not True


@pytest.mark.parametrize("open_source", ["contact", "position", "timed"])
@pytest.mark.parametrize("close_source", ["contact", "position", "timed"])
def test_open_and_close_source_combinations(evidence_type, open_source, close_source):
    evidence = evidence_type(binding(open_source=open_source, close_source=close_source, allow_estimated=True))
    evidence.note_command("open", 0, 1)
    observe(evidence, 0, position=100, open_contact="on", close_contact="on")
    state = observe(evidence, 12, position=100, open_contact="on", close_contact="on")
    assert state.open.reached is True
    assert state.open.source == open_source
    assert state.open.quality == ("estimated" if open_source == "timed" else "confirmed")
    assert state.passage_allowed is True
    assert state.close.source == close_source
    assert state.close.reached is not True


def test_released_closed_contact_does_not_prove_opening_finished(evidence_type):
    evidence = evidence_type(binding(open_source="timed", allow_estimated=True))
    evidence.note_command("open", 0, 1)
    state = observe(evidence, 3, close_contact="on")
    assert state.close.reached is False
    assert state.open.reached is None
    assert state.passage_allowed is None
    assert observe(evidence, 12, close_contact="on").passage_allowed is True
