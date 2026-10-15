"""Task completion must belong to this session, not a dock intermission."""

from __future__ import annotations

import importlib.util
import sys
from enum import IntEnum
from pathlib import Path
from types import SimpleNamespace

import pytest


PATH = Path(__file__).parents[1] / "custom_components/roborock_plus/garage_task.py"
SPEC = importlib.util.spec_from_file_location("garage_task_under_test", PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
GarageTaskSession = MODULE.GarageTaskSession
GarageTaskError = MODULE.GarageTaskError
RobotObservation = MODULE.RobotObservation


def summary(*, count=10, records=None, record=None):
    return {"clean_count": count, "records": [900] if records is None else records,
            "last_clean_record": record}


def record(*, begin=100, end=150, complete=1, finish_reason=None, error=0):
    return {"begin": begin, "end": end, "complete": complete,
            "finish_reason": finish_reason, "error": error}


def observe(session, now, *, state="charging", utc=None, data=None, clean=0,
            returning=0, trusted=True, docked=True, fresh=True, sample_id=None):
    session.observe(RobotObservation(now=now, utc=100 + now if utc is None else utc,
                    state=state, summary=data, in_cleaning=clean, in_returning=returning,
                    position_trusted=trusted, docked=docked, fresh=fresh,
                    sample_id=sample_id))


def begin_started():
    session = GarageTaskSession(dock_settle_seconds=5)
    session.begin(summary(), utc=100.8)
    observe(session, 1, state="cleaning", utc=101.2, clean=1, docked=False)
    return session


def complete_and_park(session, data=None):
    data = data or summary(count=11, records=[901, 900], record=record())
    observe(session, 51, data=data)
    observe(session, 56, data=data)


def test_first_install_requires_explicit_idle_handover():
    session = GarageTaskSession(dock_settle_seconds=5)
    assert not session.known_idle and not session.can_close
    with pytest.raises(GarageTaskError):
        session.initialize_idle()
    observe(session, 0)
    observe(session, 5)
    assert not session.known_idle and not session.can_close
    session.initialize_idle()
    assert session.known_idle and session.can_close


@pytest.mark.parametrize("field,value", [("fresh", False), ("trusted", False),
    ("docked", False), ("clean", None), ("returning", None),
    ("clean", 1), ("returning", 1), ("state", "washing_the_mop"),
    ("state", "idle"), ("state", "charging_problem"), ("state", "unknown")])
def test_initial_idle_requires_all_positive_facts(field, value):
    session = GarageTaskSession(5)
    observe(session, 0)
    observe(session, 5, **{field: value})
    with pytest.raises(GarageTaskError):
        session.initialize_idle()
    assert not session.can_close


def test_duplicates_do_not_create_second_successful_dock_sample():
    session = GarageTaskSession(5)
    observe(session, 0, sample_id="same")
    observe(session, 5, sample_id="same")
    with pytest.raises(GarageTaskError):
        session.initialize_idle()
    observe(session, 6, sample_id="new")
    session.initialize_idle()


def test_settle_window_restarts_after_bad_observation():
    session = GarageTaskSession(5)
    observe(session, 0)
    observe(session, 4, fresh=False)
    observe(session, 5)
    with pytest.raises(GarageTaskError):
        session.initialize_idle()
    observe(session, 10)
    session.initialize_idle()


def test_begin_creates_unique_session_and_rejects_duplicate_requests():
    session = begin_started()
    identifier = session.session_id
    assert identifier and session.active and session.started and not session.ended
    with pytest.raises(GarageTaskError) as exc:
        session.begin(summary(), utc=102)
    assert exc.value.reason == "task_already_active"
    assert session.session_id == identifier
    complete_and_park(session)
    session.begin(summary(count=11, records=[901, 900], record=record()), utc=200)
    assert session.session_id != identifier


@pytest.mark.parametrize("data", [None, {}, summary(count=None), summary(records=[900, 900]),
    summary(count=True), summary(records=["900"]), summary(records=[True]),
    summary(count=-1)])
def test_begin_requires_successful_summary_baseline(data):
    session = GarageTaskSession(5)
    with pytest.raises(GarageTaskError) as exc:
        session.begin(data, utc=100)
    assert exc.value.reason == "task_end_unconfirmed"
    assert not session.active


@pytest.mark.parametrize("state", ["paused", "washing_the_mop", "attaching_the_mop",
    "detaching_the_mop", "charging", "back_to_dock_washing_duster", "returning_home"])
def test_pause_wash_charge_and_task_active_off_do_not_end_session(state):
    session = begin_started()
    observe(session, 51, state=state, data=summary())
    observe(session, 56, state=state, data=summary())
    assert session.active and not session.ended and not session.can_close


def test_full_completion_requires_matching_record_and_stable_dock():
    session = begin_started()
    data = summary(count=11, records=[901, 900], record=record())
    observe(session, 51, data=data)
    assert session.active and not session.can_close
    observe(session, 56, data=data)
    assert session.ended and not session.active and session.known_idle and session.can_close
    observe(session, 57, fresh=False)
    assert not session.can_close


@pytest.mark.parametrize("data", [
    summary(count=10, records=[901, 900], record=record()),
    summary(count=12, records=[902, 901, 900], record=record()),
    summary(count=11, records=[900], record=record()),
    summary(count=11, records=[900, 901], record=record()),
    summary(count=11, records=[901, 900], record=None),
    summary(count=11, records=[901, 900], record=record(begin=99)),
    summary(count=11, records=[901, 900], record=record(begin=102)),
    summary(count=11, records=[901, 900], record=record(end=99)),
    summary(count=11, records=[901, 900], record=record(end=1000)),
    summary(count=11, records=[901, 900], record=record(complete=None)),
    summary(count=11, records=[901, 900], record=record(complete=0)),
    summary(count=11, records=[901, 900], record=record(complete=True)),
    summary(count=11, records=[901, 900], record=record(finish_reason="manual_interrupt")),
    summary(count=11, records=[901, 900], record=record(finish_reason="unknown")),
    summary(count=11, records=[901, 900], record=record(finish_reason=52)),
    summary(count=11, records=[901, 900], record=record(error=1)),
])
def test_unrelated_incomplete_ambiguous_or_old_format_records_cannot_finish(data):
    session = begin_started()
    complete_and_park(session, data)
    assert session.active and not session.ended and not session.can_close


def test_integer_second_boundary_and_objects_match_actual_dependency_fields():
    class FinishReason(IntEnum):
        finished_cleaning = 52
    session = GarageTaskSession(5)
    session.begin(SimpleNamespace(**summary()), utc=100.9)
    observe(session, 1, state="segment_cleaning", utc=101.0, clean=3, docked=False)
    data = SimpleNamespace(**summary(count=11, records=[901, 900], record=SimpleNamespace(
        **record(begin=100, finish_reason=FinishReason.finished_cleaning))))
    complete_and_park(session, data)
    assert session.can_close


def test_completed_segment_at_dock_with_pending_cleaning_is_not_whole_task():
    session = begin_started()
    data = summary(count=11, records=[901, 900], record=record())
    observe(session, 51, data=data, clean=3)
    observe(session, 56, data=data, clean=3)
    assert session.active and not session.can_close


def test_record_without_observed_start_cannot_finish_a_new_session():
    session = GarageTaskSession(5)
    session.begin(summary(), utc=100)
    complete_and_park(session)
    assert session.active and not session.started and not session.can_close


def test_explicit_cancel_requires_two_new_fresh_idle_dock_samples():
    session = begin_started()
    session.request_cancel()
    assert session.active
    observe(session, 51, clean=1)
    observe(session, 56)
    assert not session.can_close
    observe(session, 61)
    assert session.ended and session.can_close


def test_cancel_does_not_reuse_pre_request_dock_samples():
    session = begin_started()
    observe(session, 51)
    observe(session, 56)
    session.request_cancel()
    observe(session, 56)
    assert session.active and not session.can_close
    observe(session, 57)
    observe(session, 62)
    assert session.can_close


def test_external_task_after_verified_idle_invalidates_idle_knowledge():
    session = GarageTaskSession(5)
    observe(session, 0)
    observe(session, 5)
    session.initialize_idle()
    observe(session, 6, state="cleaning", clean=1, docked=False)
    observe(session, 70)
    observe(session, 75)
    assert not session.known_idle and not session.can_close


@pytest.mark.parametrize("now,utc", [(50, 90), (0, 155), (56, 1000)])
def test_clock_discontinuity_cannot_finish_session(now, utc):
    session = begin_started()
    data = summary(count=11, records=[901, 900], record=record())
    observe(session, 51, data=data)
    observe(session, now, utc=utc, data=data)
    assert session.active and not session.can_close


def test_restore_does_not_restore_dock_stability_or_permission():
    session = begin_started()
    saved = session.to_dict()
    restored = GarageTaskSession(5)
    restored.restore(saved)
    assert restored.active and restored.started and not restored.can_close
    assert restored.reason == "session_recovery_required"
    with pytest.raises(GarageTaskError):
        restored.initialize_idle()
    assert not any(key in saved for key in ("deadline", "command", "can_close", "passage_allowed"))
    complete_and_park(restored)
    assert restored.can_close


def test_restored_idle_must_observe_new_stable_dock():
    session = GarageTaskSession(5)
    observe(session, 0)
    observe(session, 5)
    session.initialize_idle()
    restored = GarageTaskSession(5)
    restored.restore(session.to_dict())
    assert restored.known_idle and not restored.can_close
    observe(restored, 0)
    assert not restored.can_close
    observe(restored, 5)
    assert restored.can_close


def test_external_task_after_completion_is_persisted_as_uncertain():
    session = begin_started()
    complete_and_park(session)
    observe(session, 57, state="cleaning", clean=1, docked=False)
    assert session.ended and not session.known_idle
    restored = GarageTaskSession(5)
    restored.restore(session.to_dict())
    observe(restored, 70)
    observe(restored, 75)
    assert not restored.can_close


def test_clock_correlation_failure_survives_restart():
    session = begin_started()
    observe(session, 2, utc=1000)
    restored = GarageTaskSession(5)
    restored.restore(session.to_dict())
    complete_and_park(restored)
    assert restored.active and not restored.can_close


def test_failed_restore_revokes_previous_idle_close_permission():
    session = GarageTaskSession(5)
    observe(session, 0)
    observe(session, 5)
    session.initialize_idle()
    assert session.can_close
    with pytest.raises(GarageTaskError):
        session.restore({"version": 2})
    assert not session.can_close


def test_invalid_utc_cannot_build_idle_stability():
    session = GarageTaskSession(5)
    observe(session, 0, utc=-100)
    observe(session, 5, utc=-95)
    with pytest.raises(GarageTaskError):
        session.initialize_idle()


@pytest.mark.parametrize("change", [{"version": 2}, {"active": "false"},
    {"active": True, "session_id": None}, {"known_idle": True, "active": True},
    {"sent_utc": float("nan")}, {"can_close": True}])
def test_corrupt_storage_is_rejected(change):
    data = begin_started().to_dict()
    data.update(change)
    restored = GarageTaskSession(5)
    with pytest.raises(GarageTaskError):
        restored.restore(data)
    assert not restored.can_close
