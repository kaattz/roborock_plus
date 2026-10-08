"""Replay the 2026-10-08 01:26 event sequence through the new phase guards.

That morning the robot never left its dock: it was attaching and detaching the
mop, and the status blipped through `docking` while `task_active` toggled. The
old automation opened and closed the door five times between 01:26:26 and
01:28:24 for no physical reason.

This takes the real observed sequence and evaluates each phase's conditions, so
a regression that lets a docked robot drive the door shows up here rather than
as a flapping cabinet door.

The condition forms were kept deliberately simple -- trigger id, entity state
equality, and a template over entity states and one cover attribute -- so this
evaluator can be faithful without embedding Jinja.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Read the config this repo builds, next to this script -- not a temp copy.
# A stale path here made the replay test an older config than the one under
# review, and report "from=None" for a fix that was already in the source.
CONFIG = json.loads(
    (Path(__file__).resolve().parent / "state_machine.json").read_text(
        encoding="utf-8"
    )
)

VACUUM = "vacuum.g20s_ultra"
STATUS = "sensor.g20s_ultra_status"
COVER = "cover.vacuum_garage_door"
TASK_ACTIVE = "binary_sensor.g20s_ultra_task_active"
CLEAR = "binary_sensor.g20s_ultra_clear_of_garage"


def eval_template(template: str, state: dict) -> bool:
    """Evaluate the subset of Jinja the config uses."""
    expr = template.strip()[2:-2].strip()

    def state_of(entity: str) -> str:
        return state.get(entity, "unknown")

    # {{ states('x') in [...] }} / not in [...]
    match = re.fullmatch(
        r"states\('([^']+)'\)\s+(not )?in\s+\[(.+)\]", expr
    )
    if match:
        entity, negated, items = match.groups()
        options = [i.strip().strip("'\"") for i in items.split(",")]
        result = state_of(entity) in options
        return (not result) if negated else result

    # {{ is_state('x', 'y') }}
    match = re.fullmatch(r"is_state\('([^']+)',\s*'([^']+)'\)", expr)
    if match:
        return state_of(match.group(1)) == match.group(2)

    # {{ (state_attr('cover', 'current_position') | float(N)) OP value }}
    match = re.fullmatch(
        r"\(state_attr\('([^']+)',\s*'([^']+)'\)\s*\|\s*float\((-?\d+)\)\)\s*"
        r"(<=|>=|<|>|==)\s*(-?\d+)",
        expr,
    )
    if match:
        entity, attribute, default, op, value = match.groups()
        raw = state.get(f"{entity}:{attribute}", default)
        try:
            left = float(raw)
        except (TypeError, ValueError):
            left = float(default)
        right = float(value)
        return {
            "<": left < right,
            ">": left > right,
            "<=": left <= right,
            ">=": left >= right,
            "==": left == right,
        }[op]

    raise AssertionError(f"unsupported template: {template}")


def condition_holds(condition: dict, trigger_id: str, state: dict) -> bool:
    """Evaluate one condition."""
    kind = condition.get("condition")
    if kind == "trigger":
        expected = condition["id"]
        expected = expected if isinstance(expected, list) else [expected]
        return trigger_id in expected
    if kind == "state":
        expected = condition["state"]
        actual = state.get(condition["entity_id"])
        return actual in expected if isinstance(expected, list) else actual == expected
    if kind == "not":
        return not all(
            condition_holds(c, trigger_id, state) for c in condition["conditions"]
        )
    if kind == "and":
        return all(
            condition_holds(c, trigger_id, state) for c in condition["conditions"]
        )
    if kind == "or":
        return any(
            condition_holds(c, trigger_id, state) for c in condition["conditions"]
        )
    if kind == "template":
        return eval_template(condition["value_template"], state)
    if kind == "numeric_state":
        attribute = condition.get("attribute")
        entity = condition["entity_id"]
        key = f"{entity}:{attribute}" if attribute else entity
        raw = state.get(key, "0")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return False
        if "below" in condition and not value < condition["below"]:
            return False
        if "above" in condition and not value > condition["above"]:
            return False
        return True
    raise AssertionError(f"unsupported condition: {condition}")


def phase_fires(branch: dict, trigger_id: str, state: dict) -> bool:
    """Return whether a choose branch's conditions all hold."""
    return all(
        condition_holds(c, trigger_id, state) for c in branch["conditions"]
    )


def sustained_trigger_fires(trigger: dict, held_seconds: float) -> bool:
    """Model a state trigger's `for:` duration.

    A trigger with `for:` only fires once the state has held that long, so a
    shorter blip produces no event at all. `held_seconds` is how long the
    observed transition actually lasted.
    """
    duration = trigger.get("for")
    if duration is None:
        return True
    required = (
        duration.get("seconds", 0)
        + duration.get("minutes", 0) * 60
        + duration.get("hours", 0) * 3600
    )
    return held_seconds >= required


def trigger_matches(trigger: dict, previous: str, current: str) -> bool:
    """Model a state trigger's `from:`/`to:` filters.

    `from:` is load-bearing for the departure trigger. The danger-zone sensor
    does not only go `on` when the robot leaves: while cleaning it alternates
    `on -> unknown -> on` as samples age out, and without a `from:` filter every
    recovery is a fresh `to: on` edge that re-runs the phase.
    """
    wanted_to = trigger.get("to")
    if wanted_to is not None:
        options = wanted_to if isinstance(wanted_to, list) else [wanted_to]
        if current not in options:
            return False
    wanted_from = trigger.get("from")
    if wanted_from is not None:
        options = wanted_from if isinstance(wanted_from, list) else [wanted_from]
        if previous not in options:
            return False
    return True


TRIGGERS_BY_ID = {t["id"]: t for t in CONFIG["triggers"]}


def sequence_closes_door(sequence: list, state: dict) -> bool:
    """Return whether a sequence actually reaches `cover.close_cover`.

    Phase D keeps its danger-zone check inside an `if/else` rather than in its
    conditions, so that a non-confirmation alerts instead of passing silently.
    That makes "the branch matched" and "the door closes" different questions,
    and the safety property is about the door -- so this walks the real steps,
    follows the branch an `if` would take, and reports what the door would do.
    """
    for step in sequence:
        if not isinstance(step, dict):
            continue
        if "if" in step:
            taken = (
                step["then"]
                if all(condition_holds(c, "", state) for c in step["if"])
                else step.get("else", [])
            )
            if sequence_closes_door(taken, state):
                return True
            continue
        if step.get("action") == "cover.close_cover":
            return True
    return False


def branch_closes_door(branch: dict, state: dict) -> bool:
    """Return whether the branch would close the door for this state."""
    return sequence_closes_door(branch["sequence"], state)


BRANCHES = CONFIG["actions"][0]["choose"]
LEAVE, RETURN, PARK = BRANCHES

# The real sequence, from entity history on 2026-10-08.
# The robot stayed docked the whole time; only the blips changed.
BASE = {
    VACUUM: "docked",
    STATUS: "charging",
    TASK_ACTIVE: "off",
    CLEAR: "on",  # the stale "clear to close" reading that caused the mess
    f"{COVER}:current_position": "0",
}

SEQUENCE = [
    ("01:26:30", "start", 0.0, {STATUS: "attaching_the_mop", TASK_ACTIVE: "on"}),
    ("01:26:34", "park_task", 0.0, {TASK_ACTIVE: "off"}),
    ("01:26:35", "start", 0.0, {STATUS: "charging"}),
    # The vacuum entity said `returning` for 2.02 s here. Modelled with the
    # real duration so the `for:` guard is actually exercised.
    (
        "01:26:37",
        "return",
        2.02,
        {STATUS: "docking", TASK_ACTIVE: "on", VACUUM: "returning"},
    ),
    ("01:26:39", "park_task", 0.0, {TASK_ACTIVE: "off", VACUUM: "docked"}),
    ("01:26:39", "park_dock", 0.0, {VACUUM: "docked", STATUS: "charging"}),
]

print("Replaying the 01:26 docked-robot flapping")
print("(clear_of_garage is the STALE 'on' that made the old automation act)")
print()

door_moves = 0
for clock, trigger, held, changes in SEQUENCE:
    state = dict(BASE)
    state.update(changes)
    if trigger not in TRIGGERS_BY_ID:
        # A state change with no trigger in this automation: mop attach/detach
        # moves `status` and `task_active` without matching any trigger id.
        print(
            f"  {clock}  {trigger:10} vacuum={state[VACUUM]:9} "
            f"status={state[STATUS]:20} -> no matching trigger"
        )
        continue
    if not sustained_trigger_fires(TRIGGERS_BY_ID[trigger], held):
        print(
            f"  {clock}  {trigger:10} held {held:4.2f}s < required "
            f"{TRIGGERS_BY_ID[trigger]['for']['seconds']}s -> trigger never fires"
        )
        continue
    fired = []
    if phase_fires(LEAVE, trigger, state):
        fired.append("LEAVE(close)")
        door_moves += 1
    if phase_fires(RETURN, trigger, state):
        fired.append("RETURN(open)")
        door_moves += 1
    if phase_fires(PARK, trigger, state):
        fired.append("PARK(close)")
        door_moves += 1
    label = ", ".join(fired) if fired else "nothing (correct)"
    print(
        f"  {clock}  {trigger:10} vacuum={state[VACUUM]:9} "
        f"status={state[STATUS]:20} -> {label}"
    )

print()
if door_moves:
    print(f"FAIL: the new guards would still move the door {door_moves} time(s).")
    raise SystemExit(1)
print("PASS: no phase fires while the robot is on its dock.")
print()

# And the phases must still work when the robot really is away.
print("Sanity: the same guards with the robot genuinely away")
away = {
    VACUUM: "cleaning",
    STATUS: "cleaning",
    TASK_ACTIVE: "on",
    CLEAR: "on",
    f"{COVER}:current_position": "100",
}
checks = [
    ("leave", LEAVE, away, True, "door open, robot out cleaning"),
    (
        "return",
        RETURN,
        {**away, VACUUM: "returning", STATUS: "returning_home",
         f"{COVER}:current_position": "0"},
        True,
        "robot returning, door shut",
    ),
    (
        "park_task",
        PARK,
        {VACUUM: "docked", STATUS: "charging", TASK_ACTIVE: "off", CLEAR: "off",
         f"{COVER}:current_position": "100"},
        True,
        "parked and confirmed inside the zone",
    ),
    (
        "park_task",
        PARK,
        {VACUUM: "docked", STATUS: "charging", TASK_ACTIVE: "off", CLEAR: "on",
         f"{COVER}:current_position": "100"},
        False,
        "parked but the sensor does NOT confirm it is inside -> refuse",
    ),
    (
        "return",
        RETURN,
        {VACUUM: "docked", STATUS: "docking", CLEAR: "on",
         f"{COVER}:current_position": "0"},
        False,
        "docked but status blips docking -> refuse to open",
    ),
]

failures = []
for trigger, branch, state, expected, description in checks:
    if branch is PARK:
        got = branch_closes_door(branch, state)
    else:
        # LEAVE and RETURN both act through their sequence once matched.
        got = phase_fires(branch, trigger, state)
    mark = "ok " if got == expected else "BAD"
    print(f"  {mark} acts={got!s:5} (want {expected!s:5})  {description}")
    if got != expected:
        failures.append(description)

print()
if failures:
    print("FAILED:")
    for item in failures:
        print(" -", item)
    raise SystemExit(1)
print("PASS: phases fire when the robot is away and refuse when it is docked.")
print()

# ---------------------------------------------------------------------------
# The observed full-chain failure, 2026-10-08 19:41-19:43.
#
# The robot left, cleaned, and came back. The danger-zone sensor alternated
# on -> unknown -> on four times while it cleaned, because a sample is only
# trusted for 2 x 5s = 10s while a map read takes a median of 6s and often 11s.
# Every recovery was a fresh `to: on` edge, so phase B ran again each time and
# the robot was paused mid-clean three times.
# ---------------------------------------------------------------------------

print("Replaying the observed on -> unknown -> on flap during one clean")
print("(each line is a sensor transition; the robot never re-entered the zone)")
print()

FLAP = [
    # (clock, previous, current, status, task_active)
    ("19:41:22", "off", "on", "cleaning", "on"),
    ("19:41:33", "on", "unknown", "cleaning", "on"),
    ("19:41:53", "unknown", "on", "cleaning", "on"),
    ("19:42:14", "on", "unknown", "cleaning", "on"),
    ("19:42:33", "unknown", "on", "cleaning", "on"),
    ("19:42:51", "on", "unknown", "cleaning", "on"),
    ("19:43:17", "unknown", "on", "returning_home", "on"),
]

# The pauses the robot actually recorded during that clean.
OBSERVED_PAUSES = ["19:42:00", "19:42:38"]

leave_fires = 0
for clock, previous, current, status, task in FLAP:
    state = {
        VACUUM: "cleaning",
        STATUS: status,
        TASK_ACTIVE: task,
        CLEAR: current,
        f"{COVER}:current_position": "100",
    }
    trigger = TRIGGERS_BY_ID["leave"]
    if not trigger_matches(trigger, previous, current):
        reason = (
            f"from={previous!r} does not match from={trigger.get('from')!r}"
        )
        print(f"  {clock}  {previous:7} -> {current:7}  no trigger ({reason})")
        continue
    fired = phase_fires(LEAVE, "leave", state)
    if fired:
        leave_fires += 1
    print(
        f"  {clock}  {previous:7} -> {current:7}  "
        f"phase B {'FIRES -> pause and close' if fired else 'declines'}"
    )

print()
print(f"phase B runs during the clean: {leave_fires} (observed pauses: "
      f"{len(OBSERVED_PAUSES)})")
if leave_fires != 1:
    print(
        f"FAIL: one departure must pause the robot once, not {leave_fires} times."
    )
    raise SystemExit(1)
print("PASS: one departure produces exactly one pause/close cycle.")
print()

# ---------------------------------------------------------------------------
# Defence in depth: even if a `to: on` edge slips through, phase B must not
# pause a robot that is already behind a closed door. The door-open condition
# is what makes a repeat harmless, so removing it would reintroduce the harm
# through a different route.
# ---------------------------------------------------------------------------

print("Repeat protection: the same edge with the door already closed")
state_door_closed = {
    VACUUM: "cleaning",
    STATUS: "cleaning",
    TASK_ACTIVE: "on",
    CLEAR: "on",
    f"{COVER}:current_position": "0",  # already shut by the first run
}
declines = not phase_fires(LEAVE, "leave", state_door_closed)
print(f"  phase B {'declines (correct)' if declines else 'FIRES (would pause again)'}")
if not declines:
    print("FAIL: a repeat would pause the robot behind a closed door.")
    raise SystemExit(1)
print("PASS: once the door is shut, a repeat edge cannot pause the robot again.")
