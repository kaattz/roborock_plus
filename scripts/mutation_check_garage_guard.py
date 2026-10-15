"""Confirm the guard tests catch the defect they were written for.

The previous version of `test_garage_guard_integration_points.py` asserted only
that `async_guard_garage_open` and `should_guard_clean_command` appeared
*somewhere* in `vacuum.py`. Those strings were present the entire time
`clean_spot` bypassed the guard, so the suite stayed green while spot cleaning
started into a shut door.

This reverts each fix in turn and requires the suite to go red, so the new tests
cannot repeat that mistake.

The baseline and mutations run in a disposable copy. Production checkout files
are only read, including when a subprocess fails, times out or is interrupted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VACUUM = REPO / "custom_components" / "roborock_plus" / "vacuum.py"
GUARD = REPO / "custom_components" / "roborock_plus" / "garage_guard.py"
TESTS = [
    "tests/test_garage_guard_integration_points.py",
    "tests/test_garage_guard.py",
]
TIMEOUT_SECONDS = 120


def revert_clean_spot(vacuum: str, guard: str) -> tuple[str, str]:
    """Put `clean_spot` back to calling send directly."""
    return (
        vacuum.replace(
            "        await self._async_guard_clean_command(RoborockCommand.APP_SPOT)\n"
            "        await self.send(RoborockCommand.APP_SPOT)\n"
            "        self._watch_clean_command(RoborockCommand.APP_SPOT)\n",
            "        await self.send(RoborockCommand.APP_SPOT)\n",
        ),
        guard,
    )


def revert_goto(vacuum: str, guard: str) -> tuple[str, str]:
    """Put goto back to calling send directly."""
    return (
        vacuum.replace(
            "        await self._async_guard_clean_command("
            "RoborockCommand.APP_GOTO_TARGET)\n"
            "        await self.send(RoborockCommand.APP_GOTO_TARGET, [x, y])\n",
            "        await self.send(RoborockCommand.APP_GOTO_TARGET, [x, y])\n",
        ),
        guard,
    )


def drop_spot_from_guard_set(vacuum: str, guard: str) -> tuple[str, str]:
    """Leave the guard call in place but stop recognising the command."""
    return vacuum, guard.replace('    "APP_SPOT",\n', "").replace('    "app_spot",\n', "")


def feed_the_wait_a_constant_elapsed(vacuum: str, guard: str) -> tuple[str, str]:
    """Pass a fixed elapsed time instead of measuring it.

    This is the mutation that escapes a source-text assertion: the wait still
    *calls* `door_reached_open_position` and the function still records
    `started = time.monotonic()`, so a check for those substrings passes while
    the gate is effectively disabled.
    """
    return vacuum, guard.replace(
        "                time.monotonic() - started,\n", "                999,\n"
    )


def drop_the_echo_gate_from_the_wait(vacuum: str, guard: str) -> tuple[str, str]:
    """Revert the post-command wait to a bare position check (the original defect)."""
    return (
        vacuum,
        guard.replace(
            "            while not door_reached_open_position(\n"
            "                time.monotonic() - started,\n"
            "                _configured_cover_position(hass, cover_entity_id),\n"
            "            ):\n",
            "            while not is_garage_door_open_enough(\n"
            "                _configured_cover_position(hass, cover_entity_id)\n"
            "            ):\n",
        ),
    )


def gate_the_precommand_check_too(vacuum: str, guard: str) -> tuple[str, str]:
    """Apply the settle gate before any command is issued.

    Gating here would idle every start for 45 seconds even with the door already
    open. The pre-command check decides from the reading's age instead.
    """
    return (
        vacuum,
        guard.replace(
            "    if window.trusts(\n"
            "        time.monotonic(),\n"
            "        _configured_cover_position(hass, cover_entity_id),\n"
            "        _configured_cover_changed_at(hass, cover_entity_id),\n"
            "    ):\n",
            "    if door_reached_open_position(\n"
            "        999, _configured_cover_position(hass, cover_entity_id)\n"
            "    ):\n",
        ),
    )


def trust_the_precommand_reading_blindly(vacuum: str, guard: str) -> tuple[str, str]:
    """THE GAP THIS CLOSES: trust any high position in the pre-check.

    The check reasoned "this call issued no command, so the reading is not an
    echo". That is about the call, not the device: the state machine's phase C
    commands the same door, the device echoes 100 while the door is physically at
    5%, and this guard then reads 100 and releases the robot at a nearly-closed
    door -- returning without calling a single service.
    """
    return (
        vacuum,
        guard.replace(
            "    if window.trusts(\n"
            "        time.monotonic(),\n"
            "        _configured_cover_position(hass, cover_entity_id),\n"
            "        _configured_cover_changed_at(hass, cover_entity_id),\n"
            "    ):\n",
            "    if is_garage_door_open_enough(\n"
            "        _configured_cover_position(hass, cover_entity_id)\n"
            "    ):\n",
        ),
    )


def ignore_another_automations_command(vacuum: str, guard: str) -> tuple[str, str]:
    """Never record that a command for this door was seen.

    The window then always takes its "no command since start-up" path, which is
    the same as the blind trust above but keeps the plumbing in place -- so it
    catches a test that only checks `trusts` is called.
    """
    return (
        vacuum,
        guard.replace("    window.note_command(time.monotonic())\n", ""),
    )


MUTATIONS = [
    ("clean_spot sends without the guard (the original defect)", revert_clean_spot),
    ("goto sends without the guard", revert_goto),
    ("APP_SPOT missing from the guarded command set", drop_spot_from_guard_set),
    (
        # The defect fixed on 2026-10-10: the device echoes the target position
        # the instant it is commanded, so a position-only wait released the
        # robot at a door that had barely started moving.
        "the post-command wait reads the echo instead of the door",
        drop_the_echo_gate_from_the_wait,
    ),
    (
        "the wait is fed a constant elapsed time",
        feed_the_wait_a_constant_elapsed,
    ),
    (
        "the pre-command check is gated behind the settle window",
        gate_the_precommand_check_too,
    ),
    (
        # The gap closed on 2026-10-14: the pre-check trusted a position that a
        # command it did not issue had just produced.
        "the pre-command check trusts another automation's echo",
        trust_the_precommand_reading_blindly,
    ),
    (
        "another automation's command is never recorded",
        ignore_another_automations_command,
    ),
]


def main() -> int:
    vacuum_original = VACUUM.read_text(encoding="utf-8")
    guard_original = GUARD.read_text(encoding="utf-8")
    caught = 0
    escaped = []

    with tempfile.TemporaryDirectory(prefix="roborock-legacy-guard-") as directory:
        workspace = Path(directory)
        component = workspace / "custom_components" / "roborock_plus"
        component.mkdir(parents=True)
        (workspace / "tests").mkdir()
        for source in VACUUM.parent.glob("*.py"):
            shutil.copyfile(source, component / source.name)
        for test in TESTS:
            shutil.copyfile(REPO / test, workspace / test)
        vacuum_path = component / "vacuum.py"
        guard_path = component / "garage_guard.py"
        # Baseline and mutations must execute the same isolated source snapshot.
        vacuum_path.write_text(vacuum_original, encoding="utf-8")
        guard_path.write_text(guard_original, encoding="utf-8")
        try:
            baseline = _run_tests(workspace)
        except subprocess.TimeoutExpired:
            print("BASELINE FAILED (timed out)")
            return 1
        if baseline.returncode != 0:
            print("BASELINE FAILED")
            print(baseline.stdout + baseline.stderr)
            return 1
        print("BASELINE PASSED", flush=True)
        for name, mutate in MUTATIONS:
            vacuum, guard = mutate(vacuum_original, guard_original)
            if vacuum == vacuum_original and guard == guard_original:
                print(f"SETUP FAILED  {name}  (anchor not found)")
                escaped.append(name)
                continue
            compile(vacuum, str(vacuum_path), "exec")
            compile(guard, str(guard_path), "exec")
            vacuum_path.write_text(vacuum, encoding="utf-8")
            guard_path.write_text(guard, encoding="utf-8")
            try:
                result = _run_tests(workspace)
            except subprocess.TimeoutExpired:
                print(f"CAUGHT        {name}  (timed out)", flush=True)
                caught += 1
                continue
            if result.returncode == 1:
                print(f"CAUGHT        {name}", flush=True)
                caught += 1
            else:
                state = "ESCAPED" if result.returncode == 0 else "HARNESS ERROR"
                print(f"{state}       {name}", flush=True)
                if result.returncode != 0:
                    print(result.stdout + result.stderr)
                escaped.append(name)

    print()
    print(f"{caught}/{len(MUTATIONS)} mutations caught")
    if escaped:
        print("escaped:")
        for name in escaped:
            print(f"  - {name}")
        return 1
    return 0


def _run_tests(workspace: Path) -> subprocess.CompletedProcess[str]:
    """Read and mutate only temporary copies; collection errors are not kills."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-q", "--no-header"],
        cwd=workspace,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


if __name__ == "__main__":
    sys.exit(main())
