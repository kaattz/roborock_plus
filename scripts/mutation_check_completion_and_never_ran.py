"""Prove the three new tests catch the defects they were written for.

Each mutation re-creates one of the bugs found on 2026-11-10 and requires the
matching test to go red. A green run therefore means something: the test is
pinned to the behaviour, not to the current text.

    python scripts/mutation_check_completion_and_never_ran.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _blueprint() -> Path:
    """The cleaning-schedule blueprint, found by content rather than by a
    filename this repo spells in Chinese.

    ``*清扫.yaml`` also matches 多阶段清扫.yaml, and picking it silently would
    make every mutation below "escape" for the wrong reason.
    """
    matches = [
        path
        for path in REPO.glob("blueprints/*.yaml")
        if "clean_started" in path.read_text(encoding="utf-8")
    ]
    if len(matches) != 1:
        raise SystemExit(
            "expected exactly one blueprint declaring clean_started, "
            f"found: {[path.name for path in matches]}"
        )
    return matches[0]


BLUEPRINT = _blueprint()
TASK_STATE = REPO / "custom_components" / "roborock_plus" / "v1_task_state.py"

PYTHON = sys.executable


def mutate(path: Path, transform) -> tuple[str, str] | None:
    """Return (original, mutated) or None when the anchor is gone."""
    original = path.read_text(encoding="utf-8")
    mutated = transform(original)
    return (original, mutated) if mutated != original else None


def run_tests(*targets: str) -> int:
    return subprocess.run(
        [PYTHON, "-m", "pytest", *targets, "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=REPO,
        capture_output=True,
        text=True,
    ).returncode


def check(name: str, path: Path, transform, tests: tuple[str, ...]) -> bool:
    pair = mutate(path, transform)
    if pair is None:
        print(f"SETUP FAILED  {name}  (anchor not found in {path.name})")
        return False
    original, mutated = pair
    backup = path.with_suffix(path.suffix + ".mutation-backup")
    shutil.copy2(path, backup)
    try:
        path.write_text(mutated, encoding="utf-8")
        code = run_tests(*tests)
    finally:
        shutil.copy2(backup, path)
        backup.unlink()
    if code == 0:
        print(f"ESCAPED       {name}")
        return False
    print(f"CAUGHT        {name}")
    return True


def drop_the_settle_wait(text: str) -> str:
    """Re-create the single-sample verdict that turned a finished clean into a failure."""
    start = text.index("  # 任务结束不等于已经回基站。")
    end = text.index("  - variables:\n      clean_minutes:")
    return text[:start] + text[end:]


def gate_never_ran_on_task_active(text: str) -> str:
    """Re-create the gate that fired 未执行 after every completed clean."""
    return text.replace(
        "    value_template: '{{ not clean_started }}'",
        "    value_template: '{{ not is_state(task_active_entity, ''on'') }}'",
    )


def forget_to_record_the_start(text: str) -> str:
    """The flag is declared but never written: every round would look 'never ran'."""
    return text.replace(
        "          - variables:\n              clean_started: true\n",
        "",
    )


def drop_returning_home_from_task_states(text: str) -> str:
    """Re-create the 4-11 s window where `task_active` fell off before the dock."""
    return text.replace('    "returning_home",\n', "")


MUTATIONS = [
    (
        "completion decided from one sample, mid-return",
        BLUEPRINT,
        drop_the_settle_wait,
        ("tests/test_blueprint_completion.py", "tests/test_blueprint_replay.py"),
    ),
    (
        "未执行 gated on task_active instead of the round's own record",
        BLUEPRINT,
        gate_never_ran_on_task_active,
        ("tests/test_blueprint_door_and_notify.py", "tests/test_blueprint_replay.py"),
    ),
    (
        "clean_started declared but never set",
        BLUEPRINT,
        forget_to_record_the_start,
        ("tests/test_blueprint_replay.py",),
    ),
    (
        "returning_home missing from the task-active state table",
        TASK_STATE,
        drop_returning_home_from_task_states,
        ("tests/test_v1_task_state.py",),
    ),
]


def main() -> int:
    if run_tests("tests/test_blueprint_completion.py", "tests/test_blueprint_replay.py",
                 "tests/test_blueprint_door_and_notify.py", "tests/test_v1_task_state.py") != 0:
        print("BASELINE IS ALREADY FAILING")
        return 1
    print("baseline: ok")

    caught = sum(check(name, path, transform, tests)
                 for name, path, transform, tests in MUTATIONS)
    print()
    print(f"{caught}/{len(MUTATIONS)} mutations caught")
    return 0 if caught == len(MUTATIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
