"""Confirm the auto-unlock tests catch the defects they were written for.

The tests were rewritten on 2026-10-14 when the judgement moved from a position
reading plus a fixed wait to the contact sensor. A rewritten test that only
asserts "the new keys are present" would stay green through every one of the old
failures, so each defect is reintroduced here and the suite must go red.

The defects, all of which shipped at some point or were one edit away:

1. Triggering on the echoed position, which fires within a second of the command
   while the door has barely moved -- the original bug, door parked at 77 and 38.
2. Acting the instant the contact flips, with no hold -- the magnet makes contact
   slightly before the door stops, so the PAUSE can still land on a moving door.
3. Matching any transition into ``off`` rather than a real open-to-closed move,
   so a BLE dropout and recovery fires it without the door having moved.
4. Letting ``unavailable`` through, so an unknown position releases the clutch --
   the dangerous direction, since a heavy door can fall.
5. Making the stop unconditional, which fires even when the door never shut.
6. Dropping the alert, so a refusal to unlock looks exactly like success.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AUTOMATION = REPO / "automations" / "vacuum_garage_door_auto_unlock.yaml"
TESTS = ["tests/test_auto_unlock_waits_for_travel.py"]
TIMEOUT_SECONDS = 120


def _drop(text: str, needle: str, replacement: str = "") -> str:
    """Replace ``needle``, raising if the anchor is gone.

    A silent no-op would be reported as an escaped mutation, which reads as a
    weak test when the real problem is a stale anchor. Fail loudly instead.
    """
    if needle not in text:
        raise SystemExit(f"anchor not found, update this script: {needle!r}")
    return text.replace(needle, replacement, 1)


def trigger_on_position(text: str) -> str:
    """Defect 1: go back to the echoed position reading."""
    return _drop(
        text,
        """- trigger: state
  entity_id: binary_sensor.vacuum_garage_door_contact_state
  # from/to 必须加引号：YAML 1.1 里裸 off/on 会被解析成布尔值，
  # 那样触发器永远匹配不上 'off' 这个字符串状态。
  from: 'on'
  to: 'off'""",
        """- trigger: numeric_state
  entity_id: cover.vacuum_garage_door
  attribute: current_position
  below: 1""",
    )


def drop_the_hold(text: str) -> str:
    """Defect 2: fire the moment the contact flips."""
    return _drop(
        text,
        """  for:
    # 10 秒做两件事：滤掉簧片抖动，并让门走完磁铁吸合后的最后一点行程。
    # 磁铁在接近关闭位时就吸合，此刻电机可能还在转最后一下 —— 等它停下来，
    # stop_cover 才落在静止的门上（那才是「松劲」，打在行进中的门上会按停）。
    seconds: 10""",
    )
def drop_the_from_guard(text: str) -> str:
    """Defect 3: match any transition into off, including a dropout recovery."""
    return _drop(text, "  from: 'on'\n")


def accept_unavailable(text: str) -> str:
    """Defect 4: treat anything that is not 'on' as safe to unlock."""
    return _drop(
        text,
        """  - condition: state
    entity_id: binary_sensor.vacuum_garage_door_contact_state
    state: 'off'""",
        """  - condition: template
    value_template: >-
      {{ states('binary_sensor.vacuum_garage_door_contact_state') != 'on' }}""",
    )


def make_the_stop_unconditional(text: str) -> str:
    """Defect 5: stop the door whether or not it is shut.

    Hoists `cover.stop_cover` out of the `if` into a bare top-level action, which
    is the shape the automation had before the guard existed.
    """
    return _drop(
        text,
        """- if:
  - condition: state
    entity_id: binary_sensor.vacuum_garage_door_contact_state
    state: 'off'
  then:
  - action: cover.stop_cover
    target:
      entity_id: cover.vacuum_garage_door
  else:""",
        """- action: cover.stop_cover
  target:
    entity_id: cover.vacuum_garage_door
- if:
  - condition: state
    entity_id: binary_sensor.vacuum_garage_door_contact_state
    state: 'off'
  then: []
  else:""",
    )


def drop_the_alert(text: str) -> str:
    """Defect 6: refuse to unlock, silently."""
    return _drop(
        text,
        """  - action: script.alert_notify
    data:
      level: warning
      title: 车库门解锁：门未确认关好""",
        """  - action: script.alert_notify_disabled_placeholder
    data:
      level: warning
      title: 车库门解锁：门未确认关好""",
    )


MUTATIONS = [
    ("trigger on the echoed position (the original defect)", trigger_on_position),
    ("act the instant the contact flips, with no hold", drop_the_hold),
    ("accept any transition into off, including a dropout", drop_the_from_guard),
    ("let unavailable through to the stop", accept_unavailable),
    ("make the stop unconditional", make_the_stop_unconditional),
    ("drop the alert on the refusal path", drop_the_alert),
]


def main() -> int:
    original = AUTOMATION.read_text(encoding="utf-8")
    caught = 0
    escaped = []

    try:
        for name, mutate in MUTATIONS:
            mutated = mutate(original)
            if mutated == original:
                print(f"SETUP FAILED  {name}  (mutation changed nothing)")
                escaped.append(name)
                continue
            AUTOMATION.write_text(mutated, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *TESTS, "-q", "--no-header"],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                print(f"CAUGHT        {name}")
                caught += 1
            else:
                print(f"ESCAPED       {name}")
                escaped.append(name)
    finally:
        AUTOMATION.write_text(original, encoding="utf-8")

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
