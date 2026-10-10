"""Confirm the guard tests catch the defect they were written for.

The previous version of `test_garage_guard_integration_points.py` asserted only
that `async_guard_garage_open` and `should_guard_clean_command` appeared
*somewhere* in `vacuum.py`. Those strings were present the entire time
`clean_spot` bypassed the guard, so the suite stayed green while spot cleaning
started into a shut door.

This reverts each fix in turn and requires the suite to go red, so the new tests
cannot repeat that mistake.
"""

from __future__ import annotations

import subprocess
import sys
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

    No command has been sent yet, so there is no echo to outlast; gating here
    would idle every start for 45 seconds even with the door already open.
    """
    return (
        vacuum,
        guard.replace(
            "    if is_garage_door_open_enough(\n"
            "        _configured_cover_position(hass, cover_entity_id)\n"
            "    ):\n",
            "    if door_reached_open_position(\n"
            "        999, _configured_cover_position(hass, cover_entity_id)\n"
            "    ):\n",
        ),
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
]


def main() -> int:
    vacuum_original = VACUUM.read_text(encoding="utf-8")
    guard_original = GUARD.read_text(encoding="utf-8")
    caught = 0
    escaped = []

    try:
        for name, mutate in MUTATIONS:
            vacuum, guard = mutate(vacuum_original, guard_original)
            if vacuum == vacuum_original and guard == guard_original:
                print(f"SETUP FAILED  {name}  (anchor not found)")
                escaped.append(name)
                continue
            VACUUM.write_text(vacuum, encoding="utf-8")
            GUARD.write_text(guard, encoding="utf-8")
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", *TESTS, "-q", "--no-header"],
                    cwd=REPO,
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                print(f"CAUGHT        {name}  (timed out)")
                caught += 1
                continue
            if result.returncode != 0:
                print(f"CAUGHT        {name}")
                caught += 1
            else:
                print(f"ESCAPED       {name}")
                escaped.append(name)
    finally:
        VACUUM.write_text(vacuum_original, encoding="utf-8")
        GUARD.write_text(guard_original, encoding="utf-8")

    print()
    print(f"{caught}/{len(MUTATIONS)} mutations caught")
    if escaped:
        print("escaped:")
        for name in escaped:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
