"""Validate the blueprint the way Home Assistant does.

The blueprint was pushed and then rejected by HA's importer with a YAML syntax
error, after a local check had claimed the templates were fine. The gap: the
local check only rendered Jinja snippets through the template engine and never
parsed the file as a Blueprint. A blueprint has two layers -- YAML structure and
the ``!input`` tag -- and neither was being exercised.

This runs the same two steps Home Assistant runs on import:

1. Parse the YAML, resolving ``!input`` into a placeholder so the structure is
   checked without needing the inputs.
2. Validate it against the blueprint schema: required metadata, every ``!input``
   name declared, and every declared input actually used.

Run directly (``python scripts/validate_blueprints.py``) or via pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
BLUEPRINTS = REPO / "blueprints"

# Inputs that are valid but may legitimately go unused as an !input because they
# are referenced through a variable of the same name. Empty by default: an
# unused input is usually a typo, and a typo means the automation silently runs
# with a default.
ALLOW_UNUSED: set[str] = set()


class InputTag(yaml.YAMLObject):
    """Stand-in for HA's ``!input name`` tag."""

    yaml_tag = "!input"

    def __init__(self, name: str) -> None:
        self.name = name

    @classmethod
    def from_yaml(cls, loader, node):
        return cls(loader.construct_scalar(node))

    @classmethod
    def to_yaml(cls, dumper, data):
        return dumper.represent_scalar(cls.yaml_tag, data.name)


def load_blueprint(path: Path) -> dict:
    """Parse a blueprint file, resolving !input into InputTag objects."""
    class Loader(yaml.SafeLoader):
        pass

    def construct_input(loader, node):
        return InputTag(loader.construct_scalar(node))

    Loader.add_constructor("!input", construct_input)
    return yaml.load(path.read_text(encoding="utf-8"), Loader=Loader)


def collect_inputs(node, found: set[str] | None = None) -> set[str]:
    """Every !input name referenced anywhere in the document."""
    if found is None:
        found = set()
    if isinstance(node, InputTag):
        found.add(node.name)
    elif isinstance(node, dict):
        for value in node.values():
            collect_inputs(value, found)
    elif isinstance(node, list):
        for value in node:
            collect_inputs(value, found)
    return found


def declared_inputs(blueprint: dict) -> set[str]:
    """Every input name declared in the blueprint schema.

    Inputs may be nested one level inside sections, which are told apart from
    plain inputs by carrying their own `input` key.
    """
    names: set[str] = set()
    for key, value in (blueprint.get("input") or {}).items():
        if isinstance(value, dict) and "input" in value:
            names.update(value["input"])
        else:
            names.add(key)
    return names


def _walk_actions(node, path: str = "actions") -> list[tuple[str, dict]]:
    """Yield every action block with a path, so structure can be checked."""
    found: list[tuple[str, dict]] = []
    if isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_walk_actions(item, f"{path}[{index}]"))
    elif isinstance(node, dict):
        found.append((path, node))
        for key in ("then", "else", "default", "sequence"):
            if key in node:
                found.extend(_walk_actions(node[key], f"{path}.{key}"))
        if "choose" in node:
            found.extend(_walk_actions(node["choose"], f"{path}.choose"))
        if "repeat" in node and isinstance(node["repeat"], dict):
            if "sequence" in node["repeat"]:
                found.extend(
                    _walk_actions(node["repeat"]["sequence"], f"{path}.repeat")
                )
    return found


def validate_action_structure(config: dict) -> list[str]:
    """Check the shapes HA's action schema requires.

    The first version of this blueprint was rejected by Home Assistant's importer
    with a YAML syntax error, even though every Jinja snippet in it rendered
    correctly. The cause was structural: ``then:`` was indented inside the
    ``if:`` condition list instead of being its sibling. Only parsing the file
    as a blueprint catches that, which is why this exists.
    """
    problems: list[str] = []
    for path, node in _walk_actions(config.get("actions")):
        # `if:` must be a list of conditions, and `then:` its sibling.
        if "if" in node and not isinstance(node["if"], list):
            problems.append(f"{path}: 'if' must be a list of conditions")
        if "if" in node and "then" not in node:
            problems.append(f"{path}: 'if' without 'then'")

        # A branch that carries a condition key inside the condition list is the
        # exact shape that produced the import failure.
        if isinstance(node.get("if"), list):
            for index, condition in enumerate(node["if"]):
                if isinstance(condition, dict) and "then" in condition:
                    problems.append(
                        f"{path}: 'then' is inside the 'if' condition list at "
                        f"index {index} -- it must be a sibling of 'if', at the "
                        "same indentation"
                    )

        # `choose` is a list of {conditions, sequence} blocks.
        if "choose" in node and not isinstance(node["choose"], list):
            problems.append(f"{path}: 'choose' must be a list")

        # `repeat` needs exactly one of count/while/until/for_each.
        if "repeat" in node:
            repeat = node["repeat"]
            if not isinstance(repeat, dict):
                problems.append(f"{path}: 'repeat' must be a mapping")
            else:
                modes = {"count", "while", "until", "for_each"} & set(repeat)
                if not modes:
                    problems.append(f"{path}: 'repeat' has no count/while/until/for_each")
                if "sequence" not in repeat:
                    problems.append(f"{path}: 'repeat' has no sequence")

        # An action must say what to do.
        if not any(
            key in node
            for key in (
                "action", "service", "if", "choose", "repeat", "delay",
                "wait_template", "wait_for_trigger", "variables", "stop",
                "parallel", "sequence", "event",
            )
        ):
            problems.append(f"{path}: no recognisable action key")

    return problems


def validate(path: Path) -> list[str]:
    """Return a list of problems; empty means the blueprint should import."""
    problems: list[str] = []
    try:
        config = load_blueprint(path)
    except yaml.YAMLError as err:
        return [f"YAML parse failed: {err}"]

    if not isinstance(config, dict) or "blueprint" not in config:
        return ["missing top-level 'blueprint:' key"]

    blueprint = config["blueprint"]
    for required in ("name", "domain"):
        if required not in blueprint:
            problems.append(f"blueprint.{required} is required")

    if blueprint.get("domain") != "automation":
        problems.append(f"unexpected domain: {blueprint.get('domain')!r}")

    declared = declared_inputs(blueprint)
    used = collect_inputs(config)
    unused = declared - used - ALLOW_UNUSED
    undeclared = used - declared
    if undeclared:
        problems.append(
            "referenced but not declared: " + ", ".join(sorted(undeclared))
        )
    if unused:
        problems.append(
            "declared but never referenced: " + ", ".join(sorted(unused))
        )

    # An automation blueprint must carry triggers and actions, or importing it
    # produces something that can never run.
    for required in ("triggers", "actions"):
        if required not in config:
            problems.append(f"missing top-level '{required}:'")

    if not isinstance(config.get("actions"), list):
        problems.append("actions must be a list")
    else:
        problems.extend(validate_action_structure(config))

    return problems


def main() -> int:
    failed = []
    for path in sorted(BLUEPRINTS.glob("*.yaml")):
        problems = validate(path)
        if problems:
            failed.append(path)
            print(f"FAIL {path.name}")
            for problem in problems:
                print(f"       {problem}")
        else:
            print(f"ok   {path.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
