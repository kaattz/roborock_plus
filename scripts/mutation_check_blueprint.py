"""Confirm the blueprint validator catches the defect it exists for.

The first version of the cleaning-schedule blueprint was reported as verified --
its Jinja snippets all rendered through the template engine -- and was then
rejected by Home Assistant's importer with a YAML syntax error. The Jinja check
had never parsed the file as a blueprint, so the structural bug was never seen.

This reintroduces the bug and requires the validator to go red, so a green run
means something.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BLUEPRINT = REPO / "blueprints" / "roborock_cleaning_schedule.yaml"

spec = importlib.util.spec_from_file_location(
    "validate_blueprints", REPO / "scripts" / "validate_blueprints.py"
)
assert spec and spec.loader
validator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = validator
spec.loader.exec_module(validator)


def _mutate(transform) -> list[str]:
    """Apply a transform to a scratch copy and return the validator's problems."""
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "blueprint.yaml"
        scratch.write_text(BLUEPRINT.read_text(encoding="utf-8"), encoding="utf-8")
        text = transform(scratch.read_text(encoding="utf-8"))
        scratch.write_text(text, encoding="utf-8")
        return validator.validate(scratch)


def reintroduce_the_indent_bug(text: str) -> str:
    """Put `then:` inside the `if:` condition list, as the original had it."""
    return text.replace(
        "        - if:\n"
        "            - condition: template\n"
        '              value_template: "{{ should_act }}"\n'
        "          then:\n",
        "        - if:\n"
        "            - condition: template\n"
        '              value_template: "{{ should_act }}"\n'
        "            then:\n",
    )


def drop_a_declared_input(text: str) -> str:
    """Declare an input that is never referenced."""
    return text.replace(
        "        mop_route:\n",
        "        mop_route_unused:\n",
        1,
    )


def break_the_yaml(text: str) -> str:
    """Unbalanced indentation that YAML cannot parse."""
    return text.replace("actions:\n", "actions:\n    - bad: [unclosed\n", 1)


MUTATIONS = [
    ("then: nested inside the if: condition list (the original defect)",
     reintroduce_the_indent_bug),
    ("a declared input that is never referenced", drop_a_declared_input),
    ("unparseable YAML", break_the_yaml),
]


def main() -> int:
    baseline = validator.validate(BLUEPRINT)
    if baseline:
        print("BASELINE IS ALREADY FAILING:")
        for problem in baseline:
            print("   ", problem)
        return 1
    print("baseline: ok")

    caught = 0
    escaped = []
    for name, transform in MUTATIONS:
        original = BLUEPRINT.read_text(encoding="utf-8")
        mutated = transform(original)
        if mutated == original:
            print(f"SETUP FAILED  {name}  (anchor not found)")
            escaped.append(name)
            continue
        problems = _mutate(transform)
        if problems:
            print(f"CAUGHT        {name}")
            print(f"                -> {problems[0][:110]}")
            caught += 1
        else:
            print(f"ESCAPED       {name}")
            escaped.append(name)

    print()
    print(f"{caught}/{len(MUTATIONS)} mutations caught")
    return 1 if escaped else 0


if __name__ == "__main__":
    sys.exit(main())
