"""Ephemeral stage receipts; never persist automation authority across reloads."""

from copy import deepcopy
import math
from typing import Any
from uuid import uuid4

from .garage_control import GarageControlError

STAGE_COMMANDS = frozenset({'app_start', 'app_segment_clean', 'app_zoned_clean'})
STAGE_SETTINGS = frozenset({'set_custom_mode', 'set_water_box_custom_mode', 'set_mop_mode', 'set_clean_repeat_times'})
_REQUIRED_SETTINGS = STAGE_SETTINGS - {'set_clean_repeat_times'}


def _json(value: Any) -> bool:
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json(item) for key, item in value.items())
    return False


def validate_stage(stage: Any) -> dict[str, Any]:
    """Reject incomplete settings rather than inheriting the previous stage."""
    if (not isinstance(stage, dict) or set(stage) - {'command', 'params', 'settings'}
        or not isinstance(stage.get('command'), str) or stage['command'] not in STAGE_COMMANDS or not _json(stage.get('params'))
        or not isinstance(stage.get('settings'), list)):
        raise GarageControlError('invalid_stage')
    supplied = set()
    for setting in stage['settings']:
        if not isinstance(setting, dict) or set(setting) != {'command', 'params'}:
            raise GarageControlError('invalid_stage')
        command, params = setting['command'], setting['params']
        if not isinstance(command, str) or command not in STAGE_SETTINGS or command in supplied:
            raise GarageControlError('invalid_stage')
        supplied.add(command)
        if command == 'set_clean_repeat_times':
            valid = (isinstance(params, dict) and set(params) == {'repeat'}
                     and type(params['repeat']) is int and params['repeat'] > 0)
        else:
            valid = isinstance(params, list) and len(params) == 1 and type(params[0]) is int
        if not valid:
            raise GarageControlError('invalid_stage')
    if not _REQUIRED_SETTINGS <= supplied or (stage['command'] == 'app_start' and 'set_clean_repeat_times' not in supplied):
        raise GarageControlError('invalid_stage')
    return deepcopy({'command': stage['command'], 'params': stage.get('params'), 'settings': stage['settings']})


def summary_signature(summary: Any) -> Any:
    """Record identity, excluding counters that may change during maintenance."""
    def value(obj: Any, key: str) -> Any:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
    record = value(summary, 'last_clean_record')
    finish = value(record, 'finish_reason')
    return deepcopy((value(summary, 'records'), value(summary, 'clean_count'),
                     tuple(value(record, key) for key in ('begin', 'end', 'complete', 'error')),
                     getattr(finish, 'name', finish)))


class GarageStage:
    """One current stage and a monotonically increasing interruption barrier."""

    def __init__(self) -> None:
        self.runtime_id = uuid4().hex
        self.session_id: str | None = None
        self.revision = 0
        self.result: str | None = None
        self.reason: str | None = None
        self.completion_signature: Any = None

    def receipt(self) -> dict[str, Any]:
        return dict(runtime_id=self.runtime_id, session_id=self.session_id, interruption_revision=self.revision)

    def check_previous(self, receipt: Any, current_session_id: str | None) -> None:
        if (not isinstance(receipt, dict) or set(receipt) != {'runtime_id', 'session_id', 'interruption_revision'}
            or type(receipt.get('interruption_revision')) is not int
            or not isinstance(receipt.get('session_id'), str) or not receipt['session_id']
            or receipt != self.receipt() or current_session_id != self.session_id or self.result != 'succeeded'):
            raise GarageControlError('invalid_stage_receipt')

    def prepare(self) -> None:
        self.session_id, self.result, self.reason = None, 'running', None
        self.completion_signature = None

    def interrupt(self, reason: str) -> None:
        self.revision += 1
        if self.result is not None:
            if self.result != 'aborted':
                self.reason = reason
            self.result = 'aborted'

    def attributes(self) -> dict[str, Any]:
        return dict(stage_runtime_id=self.runtime_id, stage_session_id=self.session_id,
                    stage_interruption_revision=self.revision, stage_result=self.result, stage_reason=self.reason)
