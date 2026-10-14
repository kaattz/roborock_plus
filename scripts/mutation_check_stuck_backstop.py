"""Confirm the backstop's tests catch the gap it was written for.

The gap is real and was measured, not imagined. With the pre-backstop tracker,
run against oscillating positions 5s apart:

    oscillation amplitude  30 -> stuck after 120s
    oscillation amplitude 190 -> stuck after 120s
    oscillation amplitude 300 -> NEVER stuck

300 units is under a second of travel at the robot's ~300 units/s cleaning speed,
so a robot rubbing against a door that did not open resets the short window's
anchor on every swing and is never reported. Each mutation below reintroduces a
real defect; the suite must go red for each, or the tests are decoration.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "custom_components" / "roborock_plus" / "v1_stuck_detection.py"
TESTS = ["tests/test_v1_stuck_detection.py"]
TIMEOUT_SECONDS = 120


def drop_the_backstop_call(text: str) -> str:
    """THE GAP: never consult the backstop, so wide oscillation is never caught."""
    return text.replace(
        "        self._observe_backstop(x=x, y=y, observed_at=observed_at)\n",
        "",
    )


def never_record_a_backstop_verdict(text: str) -> str:
    """Keep the backstop running but never let it set the verdict."""
    return text.replace(
        "            if self._stuck_since is None:\n"
        "                self._stuck_since = self._backstop_anchor.since\n",
        "",
    )


def shrink_the_backstop_radius(text: str) -> str:
    """A radius the short window already covers, so it can never fire."""
    return text.replace("BACKSTOP_RADIUS = 3000.0", "BACKSTOP_RADIUS = 200.0")


def shrink_the_backstop_window(text: str) -> str:
    """Fire almost immediately, which would flag ordinary work."""
    return text.replace(
        "BACKSTOP_WINDOW = timedelta(seconds=900)",
        "BACKSTOP_WINDOW = timedelta(seconds=5)",
    )


def never_clear_the_backstop(text: str) -> str:
    """Keep a verdict after the robot escapes, so it never recovers."""
    return text.replace(
        "            self._backstop_anchor = _Anchor(x=x, y=y, since=observed_at)\n"
        "            self._backstop_since = None\n",
        "            self._backstop_anchor = _Anchor(x=x, y=y, since=observed_at)\n",
    )


def share_one_anchor(text: str) -> str:
    """Drive the backstop from the short window's anchor.

    This is the subtle version of the gap: the code still runs, still measures a
    window, and is still reset by every swing beyond the short radius -- so it
    reproduces the original defect exactly while looking implemented.
    """
    return text.replace(
        "        distance = math.hypot(x - self._backstop_anchor.x, y - self._backstop_anchor.y)\n",
        "        distance = math.hypot(x - self._anchor.x, y - self._anchor.y)\n",
    )


MUTATIONS = [
    ("THE GAP: never consult the backstop", drop_the_backstop_call),
    ("compute the backstop but never record a verdict", never_record_a_backstop_verdict),
    ("shrink the backstop radius into the short window's range", shrink_the_backstop_radius),
    ("fire the backstop almost immediately", shrink_the_backstop_window),
    ("never clear the backstop after it escapes", never_clear_the_backstop),
    ("drive the backstop from the short window's anchor", share_one_anchor),
]


def main() -> int:
    original = MODULE.read_text(encoding="utf-8")
    caught = 0
    escaped = []

    try:
        for name, mutate in MUTATIONS:
            mutated = mutate(original)
            if mutated == original:
                print(f"SETUP FAILED  {name}  (anchor not found, update this script)")
                escaped.append(name)
                continue
            MODULE.write_text(mutated, encoding="utf-8")
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", *TESTS, "-q", "--no-header"],
                    cwd=ROOT,
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
        MODULE.write_text(original, encoding="utf-8")

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
