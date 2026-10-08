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
TESTS = ["tests/test_garage_guard_integration_points.py"]
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


MUTATIONS = [
    ("clean_spot sends without the guard (the original defect)", revert_clean_spot),
    ("goto sends without the guard", revert_goto),
    ("APP_SPOT missing from the guarded command set", drop_spot_from_guard_set),
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
