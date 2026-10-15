"""Run targeted safety mutations in a disposable copy, never in the checkout."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


REPO = Path(__file__).resolve().parents[1]
SOURCE = Path("custom_components/roborock_plus/door_evidence.py")
TEST = Path("tests/test_door_evidence.py")
TIMEOUT_SECONDS = 45

# Each replacement targets a real decision, rather than checking source strings
# in a test. Tests execute the mutated production module by path in isolation.
MUTATIONS = (
    (
        "skip full-travel wait",
        "if now - command.started_at < travel + margin:",
        "if False:",
    ),
    (
        "accept unavailable cover",
        "if cover_available is not True:",
        "if False:",
    ),
    (
        "turn estimated position into confirmation",
        'quality = "confirmed" if kind == "measured" else "estimated"',
        'quality = "confirmed"',
    ),
    (
        "turn timed arrival into confirmation",
        'return EndpointEvidence(True, "estimated", "timed", "unverified_position")',
        'return EndpointEvidence(True, "confirmed", "timed", None)',
    ),
    (
        "forget risk acceptance",
        'self._binding.get("allow_estimated") is True,',
        "True,",
    ),
    (
        "release robot while closing",
        'if self.direction == "close":',
        "if False:",
    ),
    (
        "keep command after invalidation",
        "        self._command = None\n        self._stable_since.clear()\n        self._invalid_reason = reason",
        "        self._stable_since.clear()\n        self._invalid_reason = reason",
    ),
    (
        "ignore contact polarity",
        "return value == polarity",
        'return value == "on"',
    ),
    (
        "ignore conflicting physical endpoints",
        'if all(self._contact_reached(direction, contacts[direction]) is True for direction in ("open", "close")):',
        "if False:",
    ),
    (
        "let estimate override opposite physical limit",
        "if opened.reached is True and closed.reached is True:",
        "if False:",
    ),
    (
        "reuse stable interval from previous command",
        "        self._stable_since.clear()\n        self._invalid_reason = None\n        self._command = _Command(direction, valid_now, operation_id)",
        "        self._invalid_reason = None\n        self._command = _Command(direction, valid_now, operation_id)",
    ),
    (
        "read unselected old contacts",
        'if self._binding.get(f"{direction}_source") != "contact":',
        "if False:",
    ),
)


def run_tests(workspace: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(TEST), "-q", "--maxfail=1"],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def main() -> int:
    original = (REPO / SOURCE).read_text(encoding="utf-8")
    failed = []
    with tempfile.TemporaryDirectory(prefix="roborock-door-evidence-") as directory:
        workspace = Path(directory)
        target = workspace / SOURCE
        target.parent.mkdir(parents=True)
        (workspace / TEST).parent.mkdir(parents=True)
        target.write_text(original, encoding="utf-8")
        shutil.copyfile(REPO / TEST, workspace / TEST)
        baseline = run_tests(workspace)
        if baseline.returncode != 0:
            print("BASELINE FAILED")
            print(baseline.stdout)
            print(baseline.stderr)
            return 1
        print("BASELINE PASSED", flush=True)
        for name, before, after in MUTATIONS:
            if original.count(before) != 1:
                print(f"INVALID TARGET: {name}", flush=True)
                failed.append(name)
                continue
            mutated = original.replace(before, after, 1)
            compile(mutated, str(SOURCE), "exec")
            target.write_text(mutated, encoding="utf-8")
            try:
                result = run_tests(workspace)
            except subprocess.TimeoutExpired:
                print(f"CAUGHT (timeout): {name}", flush=True)
                continue
            if result.returncode == 1:
                print(f"CAUGHT: {name}", flush=True)
            else:
                failed.append(name)
                print(f"{'ESCAPED' if result.returncode == 0 else 'HARNESS ERROR'}: {name}")
                if result.returncode != 0:
                    print(result.stdout)
                    print(result.stderr)
    print(f"{len(MUTATIONS) - len(failed)}/{len(MUTATIONS)} mutations caught")
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
