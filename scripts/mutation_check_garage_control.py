"""Verify workflow safety mutations in disposable copies, never the checkout."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = Path('custom_components/roborock_plus')
TESTS = [
    'test_garage_control.py', 'test_garage_workflow_replay.py',
    'test_garage_runtime.py', 'test_garage_runtime_safety.py',
    'test_garage_command_paths.py', 'test_resume_logic.py',
    'test_garage_transit.py',
    'test_garage_after_exit.py', 'test_garage_after_exit_review.py',
    'test_garage_after_exit_runtime.py',
]
# Targets are exact on purpose: an outdated harness fails rather than silently
# claiming coverage when the production decision no longer exists.
MUTATIONS = [
    ('garage_control.py', 'omit after-exit workflow', '        if await self._update_after_exit():', '        if False:'),
    ('garage_control.py', 'status refresh forges a fresh map crossing', 'elif token is not None and token != self._position_sample:', 'elif token is not None:'),
    ('garage_control.py', 'close after uncertain position', "and getattr(self.robot, 'outside_danger_zone', None) is True", 'and True'),
    ('garage_control.py', 'do not pause the original return', "            await self.send_robot('app_pause')", '            pass'),
    ('garage_control.py', 'skip observed return pause', '            await self._wait_robot(operation, sample, paused=True, context=context)', '            pass'),
    ('garage_control.py', 'resume return with a new charge intent', "            await self.send_robot('app_resume')", "            await self.send_robot('app_charge')"),
    ('garage_control.py', 'resume success without door permission', 'if not paused and self.passage_allowed is not True:', 'if False:'),
    ('garage_control.py', 'lose pause while the door opens', "            if direction == 'open' and self._pause_session and (", '            if False and ('),
    ('garage_control.py', 'user pause keeps automatic resume authority', '            self._user_hold = True', '            self._user_hold = False'),
    ('garage_control.py', 'skip same-task pause context validation', '            self._check_pause_context(context)', '            pass'),
    ('garage_control.py', 'adopt invalid records already present before pause', 'if not self.session.matches_current_records(summary, self.robot.utc):', 'if False:'),
    ('garage_control.py', 'launder initial invalid authority with a later good summary', "            if authority_error is not None:\n                raise GarageControlError(authority_error)\n            if context is None:\n                raise GarageControlError('session_recovery_required')", '            context = self._return_context()'),
    ('garage_control.py', 'close loop ignores a returning robot', 'if self._closing_for_exit and self._is_returning():', 'if False:'),
    ('garage_runtime.py', 'user stop leaves queued movement alive', '                self.controller.cancel_pending_for_user()', '                pass'),
    ('garage_control.py', 'navigation return keeps old endpoint semantics', "            elif command == 'app_charge' and getattr(self.session, 'task_kind', None) == 'goto':", '            elif False:'),
    ('garage_control.py', 'hide invalid task correlation from diagnostics', "        if self.session.to_dict().get('correlation_valid') is False:\n            self.fault(self.session.reason or 'session_recovery_required')\n            return", '        pass'),
    ('garage_task.py', 'external task leaves old authority reusable', 'if state in _TASK_STATES or bool(o.in_cleaning) or bool(o.in_returning):\n                self._invalidate_correlation()', 'if state in _TASK_STATES or bool(o.in_cleaning) or bool(o.in_returning):\n                self.known_idle = False\n                self.reason = "session_recovery_required"'),
    ('garage_control.py', 'new intent overwrites unknown task', 'if new_task and not self.session.can_begin:', 'if False:'),
    ('garage_control.py', 'old generation finishes closing', 'if operation is not None and operation != self.operation_id:', 'if False:'),
    ('garage_control.py', 'skip opening wait', "        await self._wait('open', operation)", '        pass'),
    ('garage_control.py', 'skip opening preflight', "        self._preflight('open')", '        pass'),
    ('garage_control.py', 'skip closing preflight', "        self._preflight('close')", '        pass'),
    ('garage_control.py', 'ignore household permission', 'if not self.close_allowed():', 'if False:'),
    ('garage_control.py', 'ignore task closing guard', 'if not (self._exit_can_close() if self._closing_for_exit else self.session.can_close):', 'if False:'),
    ('garage_control.py', 'resume without original session', 'if resume and not (self.session.active and self.session.started):', 'if False:'),
    ('garage_control.py', 'clear uncertain clock context', "if self.session.to_dict().get('correlation_valid') is False:", 'if False:'),
    ('garage_control.py', 'accept inactive returning as resumed', "and bool(getattr(self.robot,'in_cleaning',None) or getattr(self.robot,'in_returning',None))", ''),
    ('garage_control.py', 'skip durable task save', '                self.session.begin(self.robot.summary, self.robot.utc, command)\n                await self.persist()', '                self.session.begin(self.robot.summary, self.robot.utc, command)'),
    ('garage_control.py', 'skip durable navigation return save', '                self.session.redirect_to_return(self.robot.utc)\n                await self.persist()', '                self.session.redirect_to_return(self.robot.utc)'),
    ('garage_control.py', 'skip final passage recheck', "            self.refresh()\n            if self.passage_allowed is not True:\n                raise GarageControlError('feedback_unavailable')\n            self._check(operation)", '            self._check(operation)'),
    ('garage_control.py', 'wash preparation is not task start', 'or resumed or preparing:', 'or resumed:'),
    ('garage_task.py', 'any docking means task finished', '    def _matching_completion(self, summary: Any, utc: float) -> bool:\n', '    def _matching_completion(self, summary: Any, utc: float) -> bool:\n        return True\n'),
    ('garage_task.py', 'late map completion widens record window', 'first_clean_utc = o.utc if o.sample_utc is None else o.sample_utc', 'first_clean_utc = o.utc'),
    ('resume_logic.py', 'replace resume with charge', 'APP_RESUME_COMMAND = "app_resume"', 'APP_RESUME_COMMAND = "app_charge"'),
    ('garage_runtime.py', 'allow tick before session restore', 'if self._closed or not self._restored:', 'if self._closed:'),
]


def run_tests(workspace: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, '-m', 'pytest', *(str(Path('tests')/name) for name in TESTS), '-q', '--maxfail=1'],
        cwd=workspace, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45,
    )


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory(prefix='roborock-garage-workflow-') as directory:
        workspace = Path(directory)
        (workspace/MODULES).mkdir(parents=True)
        (workspace/'tests').mkdir()
        for source in (ROOT/MODULES).glob('*.py'):
            shutil.copyfile(source,workspace/MODULES/source.name)
        for name in TESTS:
            shutil.copyfile(ROOT/'tests'/name,workspace/'tests'/name)
        baseline = run_tests(workspace)
        if baseline.returncode:
            print('BASELINE FAILED\n'+baseline.stdout+baseline.stderr)
            return 1
        print('BASELINE PASSED',flush=True)
        for filename, name, before, after in MUTATIONS:
            path=workspace/MODULES/filename
            original=path.read_text(encoding='utf-8')
            if before not in original:
                failures.append(name)
                print('INVALID TARGET: '+name,flush=True)
                continue
            path.write_text(original.replace(before,after),encoding='utf-8')
            try:
                result=run_tests(workspace)
                if result.returncode==1:
                    print('CAUGHT: '+name,flush=True)
                else:
                    failures.append(name)
                    print(('ESCAPED: ' if result.returncode==0 else 'HARNESS ERROR: ')+name,flush=True)
                    if result.returncode:
                        print(result.stdout+result.stderr)
            except subprocess.TimeoutExpired:
                print('CAUGHT (timeout): '+name,flush=True)
            finally:
                path.write_text(original,encoding='utf-8')
        print(f'{len(MUTATIONS)-len(failures)}/{len(MUTATIONS)} mutations caught')
    return int(bool(failures))


if __name__=='__main__':
    raise SystemExit(main())
