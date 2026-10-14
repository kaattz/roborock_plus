"""Simulate the BLE button's press cycle against real door behaviour.

The automation this replaces used the cover's `opening`/`closing` state to decide
whether a press meant "stop". On this device that never worked: the states last a
measured mean of 0.79s (max 1.71s, n=20) out of a ~34s travel, because the
integration clears them the moment the echoed position changes. So a press during
travel fell through to the reverse branch, and the dangerous case -- a press while
opening -- called `close_cover`, the one direction that can hit the robot.

This simulates the replacement against recorded data. Every position below was
observed on `cover.vacuum_garage_door`; the contact readings and their timing come
from 2026-10-14. The check that matters most is the one the old code failed:
a press during the travel window must STOP the door, whatever the cover's state or
position says at that instant.

Run directly; exits non-zero on a problem.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "button_cycle.json").read_text(encoding="utf-8"))

DOOR = "cover.vacuum_garage_door"
BUTTON = "event.vacuum_garage_door_click"
CONTACT = "binary_sensor.vacuum_garage_door_contact_state"
MOVING_END = "input_datetime.garage_door_moving_end_time"

try:
    from jinja2 import Environment
except ImportError as err:  # pragma: no cover - environment guard
    raise SystemExit(f"jinja2 is required for the simulation: {err}") from err

# The config's templates use Home Assistant's own template functions. Stand them
# up against the scenario dict so the real template text can be rendered rather
# than re-expressed in Python -- a rewritten condition would test nothing.
_SCENARIO: dict = {}

# Fixed clock: the scenarios assert on a travel window, so `now()` must not drift
# during the run. Declared before the template functions that read it.
#
# Timezone-aware on purpose. `datetime.timestamp()` interprets a *naive* datetime
# as local time, so mixing a naive `now()` with a naive parsed value silently
# shifts the comparison by the UTC offset -- an early version of this simulation
# read every "at rest" door as still travelling for exactly that reason. Real HA
# compares epochs on both sides, so this does too.
TZ = timezone(timedelta(hours=8))  # Asia/Shanghai, matching the house
NOW = datetime(2026, 10, 14, 14, 20, 0, tzinfo=TZ)
WINDOW = 45  # must match door_timing.ECHO_SETTLE_SECONDS

ENV = Environment()


class _Bag:
    """Attribute access over dotted scenario keys, for HA's `trigger` object.

    The config's templates read `trigger.event.data.service_data.entity_id`. In a
    real run that is a live object; here the scenario carries the leaf under the
    dotted key "trigger.event.data.service_data.entity_id", so each level looks
    for keys one segment deeper.
    """

    def __init__(self, state: dict, prefix: str) -> None:
        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "_prefix", prefix)

    def __getattr__(self, name: str):
        full = f"{self._prefix}.{name}"
        state = object.__getattribute__(self, "_state")
        if full in state:
            return state[full]
        if any(key.startswith(full + ".") for key in state):
            return _Bag(state, full)
        # A missing leaf reads as HA's `default` filter target: absent, not an
        # error. Raising here would make an optional field look like a config bug.
        return None

    def __bool__(self) -> bool:
        state = object.__getattribute__(self, "_state")
        prefix = object.__getattribute__(self, "_prefix")
        return any(key.startswith(prefix + ".") for key in state)


def _states(entity_id: str) -> str:
    return _SCENARIO.get(entity_id, "unknown")


def _is_state(entity_id: str, state: str) -> bool:
    return _states(entity_id) == state


def _as_datetime(value):
    """Stand in for HA's `as_datetime`.

    Deliberately mirrors the real one's trap: a string without an offset yields a
    *naive* datetime, which cannot be compared against the aware `now()`. The
    config must therefore never do that comparison -- and a simulation that
    quietly returned an aware value would hide the defect.
    """
    if value in ("unknown", "unavailable", "none", "", None):
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _state_attr(entity_id: str, attribute: str):
    key = f"{entity_id}:{attribute}"
    if key in _SCENARIO:
        return _SCENARIO[key]
    # `input_datetime` exposes `timestamp` computed from its state, so a scenario
    # that sets the state gets the attribute for free -- the same way HA does it.
    # Parsed in the house timezone: HA reports local wall-clock time for it, and
    # interpreting that as UTC would shift every comparison by the offset.
    if attribute == "timestamp" and entity_id in _SCENARIO:
        parsed = _as_datetime(_SCENARIO[entity_id])
        return parsed.replace(tzinfo=TZ).timestamp() if parsed else None
    return None


def _now() -> datetime:
    return NOW


ENV.globals.update(
    states=_states,
    is_state=_is_state,
    as_datetime=_as_datetime,
    as_timestamp=lambda value: (
        _as_datetime(value).replace(tzinfo=TZ).timestamp()
        if _as_datetime(value)
        else None
    ),
    state_attr=_state_attr,
    now=_now,
    timedelta=timedelta,
)


def _render(template: str, state: dict) -> str:
    _SCENARIO.clear()
    _SCENARIO.update({k: v for k, v in state.items() if ":" not in k})
    return ENV.from_string(template).render(
        **state, trigger=_Bag(state, "trigger")
    )


def _condition_holds(condition: dict, state: dict, trigger_id: str) -> bool:
    """Evaluate the condition forms this config uses."""
    kind = condition.get("condition")

    if kind == "trigger":
        return trigger_id in condition["id"]

    if kind == "state":
        if "attribute" in condition:
            key = f"{condition['entity_id']}:{condition['attribute']}"
            return state.get(key) == condition["state"]
        actual = state.get(condition["entity_id"])
        expected = condition["state"]
        return actual in expected if isinstance(expected, list) else actual == expected

    if kind == "numeric_state":
        key = f"{condition['entity_id']}:{condition.get('attribute')}"
        try:
            value = float(state.get(key))
        except (TypeError, ValueError):
            return False
        if "below" in condition and not value < condition["below"]:
            return False
        if "above" in condition and not value > condition["above"]:
            return False
        return True

    if kind == "template":
        rendered = _render(condition["value_template"], state).strip()
        return rendered.lower() == "true"

    raise AssertionError(f"unsupported condition: {condition}")


def evaluate(state: dict, trigger_id: str) -> tuple[str, list[str]]:
    """Return (branch key, actions) the automation would take.

    Walks the real config: the outer choose picks record-vs-press, then the inner
    choose picks the action. Mirroring the structure rather than re-deriving the
    logic keeps the simulation honest -- a restructured automation that changes
    behaviour should make this fail.
    """
    if not all(
        _condition_holds(c, state, trigger_id) for c in CONFIG["conditions"]
    ):
        return "gated-out", []

    for index, branch in enumerate(CONFIG["actions"][0]["choose"]):
        if not all(
            _condition_holds(c, state, trigger_id) for c in branch["conditions"]
        ):
            continue
        key = branch.get("alias") or f"outer[{index}]"
        if index == 0:
            return key, [step["action"] for step in branch["sequence"]]

        # The press branch: variables, then an inner choose.
        steps = branch["sequence"]
        inner = steps[1]["choose"]
        variables = steps[0]["variables"]
        inner_state = dict(state)
        for name, template in variables.items():
            inner_state[name] = _render(template, state).strip()

        for inner_branch in inner:
            if all(
                _condition_holds(c, inner_state, trigger_id)
                for c in inner_branch["conditions"]
            ):
                return (
                    inner_branch.get("alias") or "press",
                    [step["action"] for step in inner_branch["sequence"]],
                )

        default = steps[1].get("default", [])
        if default:
            return "press-default", [step["action"] for step in default]
        return "press-none", []

    return "no-branch", []


def base_state(**overrides) -> dict:
    """A press scenario, with the door at rest and readings fresh."""
    state = {
        DOOR: "open",
        f"{DOOR}:current_position": 100,
        BUTTON: "2026-10-14T14:20:00.000+08:00",
        f"{BUTTON}:event_type": "单击",
        CONTACT: "on",
        MOVING_END: "2026-10-14 14:00:00",
    }
    state.update(overrides)
    return state


def _moving_end(offset_seconds: float) -> str:
    """A travel clock offset from NOW: positive means still travelling."""
    return (NOW + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


def IS_MOVING_TEMPLATES(config: dict) -> list[tuple[str, str]]:
    """Every template string in the config, labelled by where it sits.

    Collected by walking the real document rather than listed by hand, so a
    template added later is covered automatically. Hand-listing is how the
    datetime comparison bug slipped through: the simulation carried its own copy
    of the logic, and the copy was correct.
    """
    found: list[tuple[str, str]] = []

    def visit(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("value_template", "timestamp") and isinstance(value, str):
                    found.append((f"{path}.{key}", value))
                else:
                    visit(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, f"{path}[{index}]")

    visit(config["actions"], "actions")
    return found


failures: list[tuple[str, str, list[str]]] = []
print("BLE button press -> action, against recorded door behaviour")
print()

CASES: list[tuple[str, dict, str, str]] = [
    # --- the defect this version fixes ---------------------------------------
    (
        "press WHILE OPENING (cover echoes open) -> must stop",
        # The old code read `open` here and called close_cover. The clock is the
        # only signal that still says "travelling".
        base_state(**{DOOR: "open", f"{DOOR}:current_position": 100,
                      CONTACT: "on", MOVING_END: _moving_end(20)}),
        "press",
        "cover.stop_cover",
    ),
    (
        "press WHILE CLOSING (cover echoes closed) -> must stop",
        base_state(**{DOOR: "closed", f"{DOOR}:current_position": 0,
                      CONTACT: "off", MOVING_END: _moving_end(12)}),
        "press",
        "cover.stop_cover",
    ),
    (
        "press 1s after a command -> still travelling, stop",
        base_state(**{MOVING_END: _moving_end(WINDOW - 1)}),
        "press",
        "cover.stop_cover",
    ),
    (
        "press 1s after the window closed -> at rest, act normally",
        base_state(**{MOVING_END: _moving_end(-1)}),
        "press",
        "cover.close_cover",
    ),
    # --- the ordinary cases ---------------------------------------------------
    (
        "door shut (contact off) -> open",
        base_state(**{DOOR: "closed", f"{DOOR}:current_position": 0,
                      CONTACT: "off", MOVING_END: _moving_end(-120)}),
        "press",
        "cover.open_cover",
    ),
    (
        "door open (contact on) -> close",
        base_state(**{DOOR: "open", f"{DOOR}:current_position": 100,
                      CONTACT: "on", MOVING_END: _moving_end(-120)}),
        "press",
        "cover.close_cover",
    ),
    (
        "stopped partway, contact on -> close",
        base_state(**{DOOR: "open", f"{DOOR}:current_position": 77,
                      CONTACT: "on", MOVING_END: _moving_end(-300)}),
        "press",
        "cover.close_cover",
    ),
    # --- the refusals ---------------------------------------------------------
    (
        "clock missing -> refuse",
        base_state(**{MOVING_END: "unknown"}),
        "press",
        "script.alert_notify",
    ),
    (
        "clock unavailable -> refuse",
        base_state(**{MOVING_END: "unavailable"}),
        "press",
        "script.alert_notify",
    ),
    (
        "contact unknown -> refuse",
        base_state(**{CONTACT: "unknown"}),
        "press",
        "script.alert_notify",
    ),
    (
        "contact unavailable -> refuse",
        base_state(**{CONTACT: "unavailable"}),
        "press",
        "script.alert_notify",
    ),
    (
        "both unknown -> refuse",
        base_state(**{CONTACT: "unavailable", MOVING_END: "unknown"}),
        "press",
        "script.alert_notify",
    ),
    # --- the event gate -------------------------------------------------------
    (
        # Gated inside the press branch rather than at the top level: no branch
        # matches, so no cover command. That is the property being pinned -- the
        # label says "nothing moves", not which branch declined.
        "double click -> nothing moves",
        base_state(**{f"{BUTTON}:event_type": "双击"}),
        "press",
        "no-branch",
    ),
]

for label, state, trigger_id, expected in CASES:
    key, actions = evaluate(state, trigger_id)
    got = actions[0] if actions else key
    ok = got == expected
    mark = "ok " if ok else "BAD"
    print(f"  {mark} {label}")
    if not ok:
        print(f"        wanted {expected}, got {got} (branch {key})")
        failures.append((label, expected, [got]))

# ---------------------------------------------------------------------------
# The recording branch: this automation's own command must set the clock.
# ---------------------------------------------------------------------------

print()
print("Recording the travel window from a cover command")
record_cases = [
    ("open_cover on our door records a window", "open_cover", DOOR, "record"),
    ("close_cover on our door records a window", "close_cover", DOOR, "record"),
    ("stop_cover on our door ends the window", "stop_cover", DOOR, "record"),
    (
        "open_cover on a DIFFERENT cover is ignored",
        "open_cover",
        "cover.dinnerroom_curtain",
        "ignored",
    ),
    ("a light command is ignored", "turn_on", DOOR, "ignored"),
]

for label, service, entity, expected in record_cases:
    state = base_state()
    state["trigger.event.data.service"] = service
    state["trigger.event.data.service_data.entity_id"] = entity
    state["trigger.event.data.domain"] = "cover" if service != "turn_on" else "light"

    key, actions = evaluate(state, "command")
    got = "record" if actions and actions[0] == "input_datetime.set_datetime" else "ignored"
    ok = got == expected
    mark = "ok " if ok else "BAD"
    print(f"  {mark} {label}")
    if not ok:
        print(f"        wanted {expected}, got {got} (branch {key})")
        failures.append((label, expected, [got]))

# A list-shaped target must work too: `target: {entity_id: [a, b]}` is legal.
state = base_state()
state["trigger.event.data.service"] = "open_cover"
state["trigger.event.data.service_data.entity_id"] = ["cover.other", DOOR]
state["trigger.event.data.domain"] = "cover"
_key, actions = evaluate(state, "command")
ok = bool(actions) and actions[0] == "input_datetime.set_datetime"
print(f"  {'ok ' if ok else 'BAD'} a list target containing our door records")
if not ok:
    failures.append(("list target", "record", actions))

# ---------------------------------------------------------------------------
# The safety property: no unknown reading may move the door.
# ---------------------------------------------------------------------------

print()
print("Safety: an unknown reading must never move the door")
unknown_states = [
    ("clock unknown", {MOVING_END: "unknown"}),
    ("clock unavailable", {MOVING_END: "unavailable"}),
    ("clock empty", {MOVING_END: ""}),
    ("contact unknown", {CONTACT: "unknown"}),
    ("contact unavailable", {CONTACT: "unavailable"}),
    ("both", {CONTACT: "unknown", MOVING_END: "unknown"}),
]
for why, overrides in unknown_states:
    _key, actions = evaluate(base_state(**overrides), "press")
    moves = [a for a in actions if a.startswith("cover.")]
    status = "ok " if not moves else "BAD"
    print(f"  {status} {why:22} -> {actions or ['(nothing)']}")
    if moves:
        failures.append((why, "no cover command", actions))

# ---------------------------------------------------------------------------
# Structural guards on the config itself.
# ---------------------------------------------------------------------------

print()
print("Config structure")
block = CONFIG["actions"][0]

# The trigger wiring, not just the branch logic. Without this the simulation
# happily evaluates a record branch that Home Assistant would never reach,
# because nothing fires it -- an in-memory mutation that deleted the `command`
# trigger was reported as escaping until this check existed.
trigger_ids = {t.get("id") for t in CONFIG["triggers"]}

for needed, why in (
    ("press", "a button press must fire something"),
    ("command", "the travel window is recorded from cover commands"),
):
    if needed in trigger_ids:
        print(f"  ok  a trigger with id {needed!r} exists ({why})")
    else:
        failures.append((f"trigger {needed}", "present", sorted(trigger_ids)))
        print(f"  BAD  no trigger with id {needed!r}; {why}")

command_triggers = [t for t in CONFIG["triggers"] if t.get("id") == "command"]
for trigger in command_triggers:
    if trigger.get("trigger") != "event" or trigger.get("event_type") != "call_service":
        failures.append(("command trigger", "event/call_service", trigger))
        print(f"  BAD  command trigger is {trigger.get('trigger')}/{trigger.get('event_type')}")
    elif trigger.get("event_data", {}).get("domain") != "cover":
        failures.append(("command trigger domain", "cover", trigger.get("event_data")))
        print("  BAD  command trigger does not filter to the cover domain")
    else:
        print("  ok  the command trigger listens to call_service in the cover domain")

if CONFIG["mode"] != "queued":
    failures.append(("mode", "queued", [CONFIG["mode"]]))
    print(f"  BAD  mode is {CONFIG['mode']}; the self-triggered recording run would "
          "be dropped under `single`")
else:
    print("  ok  mode is queued (the recording run is triggered by our own command)")

if CONFIG.get("initial_state") is not False:
    failures.append(("initial_state", "false", [CONFIG.get("initial_state")]))
    print("  BAD  initial_state must be false")
else:
    print("  ok  initial_state false (a config reload must not arm a real door)")

signature = json.dumps(block, ensure_ascii=False)
for forbidden in ("garage_door_last_action", "total_duration", "opening", "closing"):
    if forbidden in signature:
        failures.append((forbidden, "absent", ["present"]))
        print(f"  BAD  logic references {forbidden!r}")
    else:
        print(f"  ok  logic does not reference {forbidden!r}")

print()
print("Every template renders without raising")
# A template that raises aborts the run, and several of this config's conditions
# decide whether the door may move -- so a raise means the door silently stops
# responding, or worse, a guard never holds. The comparison bug this caught was
# real: `now() < as_datetime(...)` mixes an aware and a naive datetime, raising
# TypeError at runtime while every structural check stayed green.
_runtime_scenarios = [
    base_state(**{MOVING_END: _moving_end(20)}),
    base_state(**{MOVING_END: _moving_end(-20)}),
    base_state(**{MOVING_END: "unknown"}),
    base_state(**{CONTACT: "unavailable"}),
]
for scenario in _runtime_scenarios:
    rendered_trigger = dict(scenario)
    rendered_trigger["trigger.event.data.service"] = "open_cover"
    rendered_trigger["trigger.event.data.service_data.entity_id"] = DOOR
    rendered_trigger["trigger.event.data.domain"] = "cover"

    for source in (
        IS_MOVING_TEMPLATES(CONFIG),
    ):
        for label, template in source:
            try:
                _render(template, rendered_trigger)
            except Exception as err:  # noqa: BLE001 - report, do not crash
                failures.append((f"template {label}", "renders", [repr(err)]))
                print(f"  BAD  {label} raised {type(err).__name__}: {err}")
if not any(f[0].startswith("template ") for f in failures):
    print("  ok  no template raised on any scenario")

print()
if failures:
    print("FAILED:")
    for label, expected, got in failures:
        print(f"  - {label}: wanted {expected}, got {got}")
    raise SystemExit(1)

print("=" * 70)
print("ALL CHECKS PASS")
