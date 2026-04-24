from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "safe_zone_entities.py"
)
SPEC = spec_from_file_location("roborock_plus_safe_zone_entities", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

build_safe_zone_entities = MODULE.build_safe_zone_entities


def test_build_safe_zone_entities_returns_flat_list() -> None:
    coordinators = ["a", "b"]

    try:
        entities = build_safe_zone_entities(
            coordinators,
            lambda coordinator: f"has:{coordinator}",
            lambda coordinator: f"in:{coordinator}",
            lambda coordinator: f"clear:{coordinator}",
        )
    except TypeError as err:
        raise AssertionError("clear-of-garage entity factory is missing") from err

    assert entities == [
        "has:a",
        "in:a",
        "clear:a",
        "has:b",
        "in:b",
        "clear:b",
    ]
