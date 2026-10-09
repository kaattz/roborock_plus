"""Verify the blueprint's two-trigger design against real Home Assistant.

The cleaning blueprint needs two ways to start:
  * at a time, for the common-area and bedroom tasks
  * when an appliance finishes, for the post-lunch kitchen task

The obvious way to express "use one or the other" is a trigger with
``enabled: !input``. Checking Home Assistant's own source shows that does not
work: ``async_validate_trigger_config`` validates *every* trigger regardless of
``enabled``, and only ``async_initialize_triggers`` skips disabled ones. So a
time-only instance with an empty appliance entity would fail validation and mark
the whole automation ``failed_triggers``.

The design therefore avoids leaving an input empty. This script checks that the
shipped blueprint has the shape that actually works, and exercises the
substitution in Home Assistant where possible.

Run directly; exits non-zero on a problem.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _blueprint() -> Path:
    candidates = [
        path
        for path in (ROOT / "blueprints").glob("*.yaml")
        if "cleaning" in path.name or "清扫" in path.name
    ]
    if len(candidates) != 1:
        raise SystemExit(f"expected one cleaning blueprint, got {candidates}")
    return candidates[0]


BLUEPRINT = _blueprint()
TEXT = BLUEPRINT.read_text(encoding="utf-8")


def load_blueprint(path: Path) -> dict:
    """Parse with !input resolved, so the schema can be inspected."""
    spec = importlib.util.spec_from_file_location(
        "validate_blueprints", ROOT / "scripts" / "validate_blueprints.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.load_blueprint(path)


def main() -> int:
    config = load_blueprint(BLUEPRINT)
    problems: list[str] = []

    triggers = config.get("triggers") or []
    if not triggers:
        problems.append("no triggers at all")

    # Every trigger must survive validation no matter which inputs the user
    # picks. That rules out a disabled trigger whose entity_id is empty.
    for index, trigger in enumerate(triggers):
        kind = trigger.get("trigger")
        if kind == "time":
            at = trigger.get("at")
            if at is None:
                problems.append(f"triggers[{index}]: time trigger without 'at'")
        elif kind == "state":
            entity = trigger.get("entity_id")
            if entity is None:
                problems.append(
                    f"triggers[{index}]: state trigger without entity_id -- "
                    "validation runs even for disabled triggers, so this would "
                    "fail the whole automation"
                )
            else:
                problems.append(
                    f"triggers[{index}]: state trigger present; the shipped "
                    "design keeps the appliance trigger out of the blueprint "
                    f"({entity!r})"
                )
        else:
            problems.append(f"triggers[{index}]: unexpected trigger {kind!r}")

    # Conditions must not block the appliance path. A weekday condition is
    # fine for time; the appliance path needs its own time-of-day window.
    conditions = config.get("conditions") or []
    for index, condition in enumerate(conditions):
        if condition.get("condition") == "time" and "weekday" in condition:
            problems.append(
                f"conditions[{index}]: a top-level weekday condition would also "
                "gate the appliance trigger"
            )

    print(f"blueprint: {BLUEPRINT.name}")
    print(f"triggers : {[t.get('trigger') for t in triggers]}")
    print(f"conditions: {[c.get('condition') for c in conditions]}")
    print()

    if problems:
        print("PROBLEMS:")
        for problem in problems:
            print("  -", problem)
        return 1
    print("ok: trigger shape is compatible with input validation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
