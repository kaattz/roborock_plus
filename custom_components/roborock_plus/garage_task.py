"""Conservative session tracking for robot garage tasks."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any
from uuid import uuid4


_CLEANING_STATES = frozenset({"cleaning", "spot_cleaning", "zoned_cleaning", "segment_cleaning"})
# Washing, attaching mops and "charging_problem" do not prove a settled idle dock.
_IDLE_DOCK_STATES = frozenset({"charging", "charging_complete"})
_TASK_STATES = _CLEANING_STATES | frozenset({
    "starting", "paused", "docking", "returning_home", "going_to_target",
    "washing_the_mop", "washing_the_mop_2", "going_to_wash_the_mop",
    "back_to_dock_washing_duster", "attaching_the_mop", "detaching_the_mop",
    "remote_control_active", "manual_mode", "emptying_the_bin",
})
# Names from python-roborock 5.5.1; never infer meaning from a bare integer.
_SUCCESS_FINISH_REASONS = frozenset({
    "finished_cleaning", "finished_cleaning_2", "finished_cleaning_3",
    "finished_cleaning_4", "finished_clenaing_5",
})
_SESSION_FIELDS = frozenset({
    "version", "session_id", "active", "started", "ended", "known_idle",
    "sent_utc", "first_clean_utc", "baseline_records", "baseline_clean_count",
    "baseline_record", "observed_record_id", "cancel_requested", "correlation_valid", "reason",
    "task_kind", "first_transit_utc",
})


def _value(data: Any, name: str, default: Any = None) -> Any:
    return data.get(name, default) if isinstance(data, Mapping) else getattr(data, name, default)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _zero(value: Any) -> bool:
    return _integer(value) and value == 0


def _records(value: Any) -> bool:
    return isinstance(value, list) and all(_integer(item) and item >= 0 for item in value) and len(set(value)) == len(value)


def _baseline(summary: Any) -> tuple[list[int], int, dict[str, int | None] | None]:
    records = _value(summary, "records")
    count = _value(summary, "clean_count")
    if not _records(records) or not _integer(count) or count < 0:
        raise GarageTaskError("task_end_unconfirmed")
    record = _value(summary, "last_clean_record")
    saved = None
    if record is not None:
        saved = {name: _value(record, name) for name in ("begin", "end")}
        if any(value is not None and (not _integer(value) or value < 0) for value in saved.values()):
            raise GarageTaskError("task_end_unconfirmed")
    return list(records), count, saved


def normalize_session_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate persisted facts without granting a command or close permission."""
    if isinstance(payload, Mapping) and set(payload) == _SESSION_FIELDS - {"task_kind", "first_transit_utc"}:
        payload = {**payload, "task_kind": "clean", "first_transit_utc": None}
    if not isinstance(payload, Mapping) or set(payload) != _SESSION_FIELDS:
        raise GarageTaskError("session_recovery_required")
    data = deepcopy(dict(payload))
    if type(data["version"]) is not int or data["version"] != 1:
        raise GarageTaskError("session_recovery_required")
    if any(type(data[key]) is not bool for key in ("active", "started", "ended", "known_idle", "cancel_requested", "correlation_valid")):
        raise GarageTaskError("session_recovery_required")
    sid = data["session_id"]
    if sid is not None and (not isinstance(sid, str) or not sid.strip()):
        raise GarageTaskError("session_recovery_required")
    if data["reason"] is not None and not isinstance(data["reason"], str):
        raise GarageTaskError("session_recovery_required")
    if data["task_kind"] not in ("clean", "return", "goto"):
        raise GarageTaskError("session_recovery_required")
    for key in ("sent_utc", "first_clean_utc", "first_transit_utc"):
        if data[key] is not None and (not _finite(data[key]) or data[key] < 0):
            raise GarageTaskError("session_recovery_required")
    if not _records(data["baseline_records"]):
        raise GarageTaskError("session_recovery_required")
    count = data["baseline_clean_count"]
    if count is not None and (not _integer(count) or count < 0):
        raise GarageTaskError("session_recovery_required")
    baseline_record = data["baseline_record"]
    if baseline_record is not None and (
        not isinstance(baseline_record, dict) or set(baseline_record) != {"begin", "end"}
        or any(value is not None and (not _integer(value) or value < 0) for value in baseline_record.values())
    ):
        raise GarageTaskError("session_recovery_required")
    record_id = data["observed_record_id"]
    if record_id is not None and (not _integer(record_id) or record_id < 0):
        raise GarageTaskError("session_recovery_required")
    if (data["active"] and (sid is None or data["known_idle"] or data["ended"])) or (
        data["ended"] and sid is None
    ):
        raise GarageTaskError("session_recovery_required")
    if sid is None:
        if data["task_kind"] != "clean":
            raise GarageTaskError("session_recovery_required")
        if any(data[key] for key in ("active", "started", "ended", "cancel_requested", "baseline_records")) or any(
            data[key] is not None for key in ("sent_utc", "first_clean_utc", "baseline_clean_count", "baseline_record", "observed_record_id")
        ):
            raise GarageTaskError("session_recovery_required")
    elif data["sent_utc"] is None or count is None or not (data["active"] or data["ended"]):
        raise GarageTaskError("session_recovery_required")
    first = data["first_clean_utc"] if data["task_kind"] == "clean" else data["first_transit_utc"]
    other = data["first_transit_utc"] if data["task_kind"] == "clean" else data["first_clean_utc"]
    if other is not None or data["started"] != (first is not None):
        raise GarageTaskError("session_recovery_required")
    if data["started"] and math.floor(first) < math.floor(data["sent_utc"]):
        raise GarageTaskError("session_recovery_required")
    return data


@dataclass(frozen=True)
class RobotObservation:
    """One successful status poll with an independently checked position."""

    now: float
    utc: float
    state: str
    in_cleaning: int | None = None
    in_returning: int | None = None
    summary: Any = None
    position_trusted: bool = False
    docked: bool = False
    fresh: bool = True
    sample_id: Any = None
    sample_utc: float | None = None
    outside_danger_zone: bool | None = None
    position_sample_id: Any = None


class GarageTaskError(ValueError):
    """A task request or stored session cannot be accepted."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GarageTaskSession:
    """Track only session facts; never issue or replay commands."""

    def __init__(self, dock_settle_seconds: float) -> None:
        if not _finite(dock_settle_seconds) or dock_settle_seconds < 0:
            raise GarageTaskError("invalid_dock_settle_seconds")
        self.dock_settle_seconds = float(dock_settle_seconds)
        self.session_id: str | None = None
        self.active = False
        self.started = False
        self.ended = False
        self.known_idle = False
        self.reason: str | None = "session_recovery_required"
        self._sent_utc: float | None = None
        self._first_clean_utc: float | None = None
        self._baseline_records: list[int] = []
        self._baseline_clean_count: int | None = None
        self._baseline_record: dict[str, int | None] | None = None
        self._observed_record_id: int | None = None
        self._cancel_requested = False
        self._correlation_invalid = False
        self.task_kind = "clean"
        self._first_transit_utc: float | None = None
        self._reset_observation()

    def _reset_observation(self) -> None:
        self._last_clock: tuple[float, float] | None = None
        self._last_sample: Any = None
        self._dock_since: float | None = None
        self._dock_samples = 0
        self._dock_stable = False
        self._latest_quiet = False
        self._transit_idle_since: float | None = None
        self._transit_idle_samples = 0

    @property
    def can_begin(self) -> bool:
        """A new explicit intent cannot erase an unknown external task."""
        terminal_goto = self.task_kind == "goto" and self.ended and self.reason is None
        return not self.active and not self._correlation_invalid and self._latest_quiet and (self.known_idle or terminal_goto)

    @property
    def can_close(self) -> bool:
        """Return a current task-side guard; other guards still apply."""
        return self.known_idle and not self.active and self._dock_stable and not self._correlation_invalid

    def initialize_idle(self) -> None:
        """Explicit first-install handover after independently verified idle."""
        if self.active or not self._dock_stable or self._correlation_invalid:
            raise GarageTaskError("session_recovery_required")
        self.known_idle = True
        self.reason = None

    def reset_to_idle(self) -> None:
        """Replace a finished session with a clean idle one.

        Used when a stored record cannot be trusted but nothing is in play: the
        robot is parked and no task is running, so there is no live fact to
        contradict. Without this the unusable record survives every restart and
        re-raises the same fault that the operator's reset cannot clear.

        Refuses whenever the task is still active. That is the one fact this
        object can check by itself, and losing a live task record would mean
        losing the ability to say "end this task before closing the door".

        ``known_idle`` is deliberately NOT required here. A record whose
        correlation broke often also failed to establish idleness, so demanding
        it made this method unreachable for exactly the records it exists to
        clear. Whether the robot is really parked is decided by the caller from
        the live observation, which is the authority anyway.
        """
        if self.active:
            raise GarageTaskError("session_recovery_required")
        self.session_id = None
        self.started = False
        self.ended = False
        self._sent_utc = None
        self._first_clean_utc = None
        self._baseline_records = ()
        self._baseline_clean_count = None
        self._baseline_record = None
        self._observed_record_id = None
        self._cancel_requested = False
        self._correlation_invalid = False
        self.task_kind = "clean"
        self._first_transit_utc = None
        self.reason = None
        # The caller only reaches here with a live observation of a parked robot,
        # which is the same evidence initialize_idle would accept.
        self.known_idle = True
        self._reset_observation()

    def begin(self, summary: Any, utc: float, command: str = "app_start") -> None:
        """Save a fresh pre-send baseline. The caller sends the command later."""
        if self.active:
            raise GarageTaskError("task_already_active")
        if not _finite(utc) or utc < 0 or not isinstance(command, str) or not command:
            raise GarageTaskError("task_end_unconfirmed")
        records, count, baseline_record = _baseline(summary)
        self.session_id = uuid4().hex
        self.active, self.started, self.ended, self.known_idle = True, False, False, False
        self._sent_utc = float(utc)
        self._first_clean_utc = None
        self.task_kind = "return" if command == "app_charge" else "goto" if command == "app_goto_target" else "clean"
        self._first_transit_utc = None
        self._transit_idle_since, self._transit_idle_samples = None, 0
        self._baseline_records, self._baseline_clean_count = records, count
        self._baseline_record = baseline_record
        self._observed_record_id = None
        self._cancel_requested = False
        self._correlation_invalid = False
        self._dock_since, self._dock_samples, self._dock_stable = None, 0, False
        self.reason = "task_end_unconfirmed"

    def request_cancel(self) -> None:
        """Record this session's stop intent, without assuming it succeeded."""
        if not self.active:
            raise GarageTaskError("no_active_task")
        self._cancel_requested = True
        self._dock_since, self._dock_samples, self._dock_stable = None, 0, False

    def redirect_to_return(self, utc: float) -> None:
        """Replace a managed navigation intent with the user's explicit return."""
        if (not self.active or self.task_kind != "goto" or self._correlation_invalid
            or self._cancel_requested or not _finite(utc) or utc < 0):
            raise GarageTaskError("session_recovery_required")
        self.task_kind = "return"
        self._sent_utc = float(utc)
        self.started = False
        self._first_transit_utc = None
        self._dock_since, self._dock_samples, self._dock_stable = None, 0, False
        self._transit_idle_since, self._transit_idle_samples = None, 0
        self.reason = "task_end_unconfirmed"

    def reconcile_active(self, observation: RobotObservation) -> None:
        """Explicitly match a restored task to live state, without resuming it."""
        state = str(getattr(observation.state, "name", observation.state))
        active_flag = any(_integer(flag) and flag > 0 for flag in (
            observation.in_cleaning, observation.in_returning
        ))
        terminal_goto = self.task_kind == "goto" and self.ended and not self.active
        live_goto = self.task_kind == "goto" and state == "going_to_target"
        live_return = self.task_kind == "return" and state in ("docking", "returning_home")
        if terminal_goto:
            state_valid = state == "idle" and observation.position_trusted and _zero(observation.in_cleaning) and _zero(observation.in_returning)
        else:
            state_valid = (state in _TASK_STATES and state not in ("manual_mode", "remote_control_active", "emptying_the_bin")
                           and (state == "paused" or active_flag or live_goto or live_return))
        if (not observation.fresh or self._correlation_invalid or (not self.active and not terminal_goto)
            or not self.started or self._cancel_requested
            or not state_valid):
            raise GarageTaskError("session_recovery_required")
        try:
            records, count, detail = _baseline(observation.summary)
        except GarageTaskError as err:
            raise GarageTaskError("session_recovery_required") from err
        if (records != self._baseline_records or count != self._baseline_clean_count
            or detail != self._baseline_record):
            raise GarageTaskError("session_recovery_required")
        self.reason = None if terminal_goto else "task_end_unconfirmed"

    def observe(self, observation: RobotObservation) -> None:
        """Consume a genuinely new poll; no transition here moves hardware."""
        o = observation
        state = str(getattr(o.state, "name", o.state))
        self._latest_quiet = bool(o.fresh and o.position_trusted and state in ("idle", "charging", "charging_complete")
                                  and _zero(o.in_cleaning) and _zero(o.in_returning))
        if not _finite(o.now) or not _finite(o.utc) or o.utc < 0:
            self._invalidate_correlation()
            return
        sample = o.sample_id if o.sample_id is not None else (o.now, o.utc)
        if not o.fresh:
            self._dock_since, self._dock_samples, self._dock_stable = None, 0, False
            self._transit_idle_since, self._transit_idle_samples = None, 0
            return
        if sample == self._last_sample:
            return
        if self._last_clock is not None:
            old_now, old_utc = self._last_clock
            if o.now <= old_now or o.utc < old_utc or abs((o.utc - old_utc) - (o.now - old_now)) > 2:
                self._invalidate_correlation()
                return
        self._last_sample, self._last_clock = sample, (o.now, o.utc)
        if self.active and self._sent_utc is not None and math.floor(o.utc) < math.floor(self._sent_utc):
            self._invalidate_correlation()
            return
        if self.active and self.task_kind != "clean":
            actual_utc = o.utc if o.sample_utc is None else o.sample_utc
            if not _finite(actual_utc) or self._sent_utc is None or not self._sent_utc <= actual_utc <= o.utc:
                self._dock_since, self._dock_samples, self._dock_stable = None, 0, False
                self._transit_idle_since, self._transit_idle_samples = None, 0
                return
        state = str(getattr(o.state, "name", o.state))
        idle = (o.docked is True and o.position_trusted is True and state in _IDLE_DOCK_STATES
                and _zero(o.in_cleaning) and _zero(o.in_returning))
        if idle and not self._correlation_invalid:
            if self._dock_since is None:
                self._dock_since = o.now
            self._dock_samples += 1
            self._dock_stable = self._dock_samples >= 2 and o.now - self._dock_since >= self.dock_settle_seconds
        else:
            self._dock_since, self._dock_samples, self._dock_stable = None, 0, False
        if not self.active:
            if state in _TASK_STATES or bool(o.in_cleaning) or bool(o.in_returning):
                self._invalidate_correlation()
            return
        if self._correlation_invalid:
            return
        if self.task_kind != "clean":
            self._observe_transit(o, state, idle)
            return
        if not self.started and state in _CLEANING_STATES:
            # Consumption can follow slow map work. Correlate records against
            # the actual status RPC, without mixing its age into clock checks.
            first_clean_utc = o.utc if o.sample_utc is None else o.sample_utc
            if (
                self._sent_utc is None
                or not isinstance(first_clean_utc, (int, float))
                or isinstance(first_clean_utc, bool)
                or not self._sent_utc <= first_clean_utc <= o.utc
                or not _finite(first_clean_utc)
            ):
                self._invalidate_correlation()
                return
            self.started = True
            self._first_clean_utc = float(first_clean_utc)
        if self._dock_stable and (self._cancel_requested or self._matching_completion(o.summary, o.utc)):
            self.active, self.ended, self.known_idle = False, True, True
            self.reason = None

    def _observe_transit(self, observation: RobotObservation, state: str, idle_dock: bool) -> None:
        o = observation
        sample_utc = o.utc if o.sample_utc is None else o.sample_utc
        if not _finite(sample_utc) or self._sent_utc is None or not self._sent_utc <= sample_utc <= o.utc:
            return
        returning = state in ("docking", "returning_home") and _integer(o.in_returning) and o.in_returning > 0
        if not self.started and ((self.task_kind == "return" and (returning or idle_dock))
                                 or (self.task_kind == "goto" and state == "going_to_target")):
            self.started = True
            self._first_transit_utc = float(sample_utc)
        if self._dock_stable and (self._cancel_requested or (self.task_kind == "return" and self.started)):
            self.active, self.ended, self.known_idle = False, True, True
            self.reason = None
            return
        arrived = (self.task_kind == "goto" and self.started and self._first_transit_utc is not None
                   and sample_utc > self._first_transit_utc and self._latest_quiet and state == "idle")
        if not arrived:
            self._transit_idle_since, self._transit_idle_samples = None, 0
            return
        if self._transit_idle_since is None:
            self._transit_idle_since = o.now
        self._transit_idle_samples += 1
        if self._transit_idle_samples >= 2 and o.now - self._transit_idle_since >= self.dock_settle_seconds:
            self.active, self.ended, self.known_idle = False, True, False
            self.reason = None

    def _invalidate_correlation(self) -> None:
        """Revoke this session's authority after a clock or task-lineage break."""
        self._correlation_invalid = True
        self.known_idle = False
        self._dock_since, self._dock_samples, self._dock_stable = None, 0, False
        self._transit_idle_since, self._transit_idle_samples = None, 0
        self.reason = "task_end_unconfirmed" if self.active else "session_recovery_required"

    def matches_current_records(self, summary: Any, utc: float) -> bool:
        """Check against the durable pre-send baseline, not a new pause snapshot."""
        if not (self.active and self.started and not self._cancel_requested and not self._correlation_invalid):
            return False
        try:
            records, count, detail = _baseline(summary)
        except GarageTaskError:
            return False
        unchanged = (records == self._baseline_records and count == self._baseline_clean_count
                     and detail == self._baseline_record)
        return unchanged or self.matches_current_completion(summary, utc)

    def matches_current_completion(self, summary: Any, utc: float) -> bool:
        """Allow a delayed successful record for this task, never another task."""
        return bool(self.active and self.started and self.task_kind == "clean"
                    and not self._cancel_requested and not self._correlation_invalid
                    and self._matching_completion(summary, utc))

    def _matching_completion(self, summary: Any, utc: float) -> bool:
        if not self.started or self._first_clean_utc is None or self._sent_utc is None:
            return False
        try:
            records, count, _ = _baseline(summary)
        except GarageTaskError:
            return False
        candidates = set(records).difference(self._baseline_records)
        if count != self._baseline_clean_count + 1 or len(candidates) != 1 or not records or records[0] not in candidates:
            return False
        detail = _value(summary, "last_clean_record")
        begin, end = _value(detail, "begin"), _value(detail, "end")
        complete = _value(detail, "complete")
        finish = _value(detail, "finish_reason")
        finish_name = getattr(finish, "name", finish)
        error = _value(detail, "error")
        if not (_integer(begin) and _integer(end) and _integer(complete) and complete == 1):
            return False
        if not (math.floor(self._sent_utc) <= begin <= math.floor(self._first_clean_utc) and begin <= end <= math.floor(utc)):
            return False
        if finish is not None and (not isinstance(finish_name, str) or finish_name not in _SUCCESS_FINISH_REASONS):
            return False
        if error is not None and not _zero(error):
            return False
        if self._baseline_record is not None and begin == self._baseline_record["begin"]:
            return False
        self._observed_record_id = records[0]
        return True

    def to_dict(self) -> dict[str, Any]:
        """Serialize facts only: no waits, deadlines, commands or permission."""
        return deepcopy({
            "version": 1, "session_id": self.session_id, "active": self.active,
            "started": self.started, "ended": self.ended, "known_idle": self.known_idle,
            "sent_utc": self._sent_utc, "first_clean_utc": self._first_clean_utc,
            "baseline_records": self._baseline_records, "baseline_clean_count": self._baseline_clean_count,
            "baseline_record": self._baseline_record, "observed_record_id": self._observed_record_id,
            "cancel_requested": self._cancel_requested,
            "correlation_valid": not self._correlation_invalid, "reason": self.reason,
            "task_kind": self.task_kind, "first_transit_utc": self._first_transit_utc,
        })

    def restore(self, payload: Mapping[str, Any]) -> None:
        """Restore correlation facts and require new live observations."""
        self._correlation_invalid = True
        self._reset_observation()
        self.reason = "session_recovery_required"
        data = normalize_session_payload(payload)
        for key in ("session_id", "active", "started", "ended", "known_idle", "task_kind"):
            setattr(self, key, data[key])
        for key in ("sent_utc", "first_clean_utc", "baseline_records", "baseline_clean_count",
                    "baseline_record", "observed_record_id", "cancel_requested", "first_transit_utc"):
            setattr(self, f"_{key}", data[key])
        self.reason = "session_recovery_required"
        self._correlation_invalid = not data["correlation_valid"]
        self._reset_observation()
