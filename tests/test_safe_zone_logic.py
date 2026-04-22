from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "roborock_plus"
    / "safe_zone.py"
)
SPEC = spec_from_file_location("roborock_plus_safe_zone", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


SafeZone = MODULE.SafeZone
suggest_safe_zone = MODULE.suggest_safe_zone
point_in_safe_zone = MODULE.point_in_safe_zone
DEFAULT_DOCK_X = MODULE.DEFAULT_DOCK_X
DEFAULT_DOCK_Y = MODULE.DEFAULT_DOCK_Y


def test_suggest_safe_zone_east() -> None:
    zone = suggest_safe_zone(
        dock_x=DEFAULT_DOCK_X,
        dock_y=DEFAULT_DOCK_Y,
        cabinet_direction="east",
        safe_distance_front=2500,
        safe_half_width=1200,
        close_margin=300,
    )
    assert zone == SafeZone(
        min_x=25800,
        max_x=28300,
        min_y=24300,
        max_y=26700,
    )


def test_suggest_safe_zone_north() -> None:
    zone = suggest_safe_zone(
        dock_x=DEFAULT_DOCK_X,
        dock_y=DEFAULT_DOCK_Y,
        cabinet_direction="north",
        safe_distance_front=2500,
        safe_half_width=1200,
        close_margin=300,
    )
    assert zone == SafeZone(
        min_x=24300,
        max_x=26700,
        min_y=22700,
        max_y=25200,
    )


def test_point_in_safe_zone_true() -> None:
    zone = SafeZone(min_x=25800, max_x=28300, min_y=24300, max_y=26700)
    assert point_in_safe_zone(26000, 25000, zone) is True


def test_point_in_safe_zone_false() -> None:
    zone = SafeZone(min_x=25800, max_x=28300, min_y=24300, max_y=26700)
    assert point_in_safe_zone(25000, 25000, zone) is False


def test_suggest_safe_zone_rejects_invalid_direction() -> None:
    try:
        suggest_safe_zone(
            dock_x=DEFAULT_DOCK_X,
            dock_y=DEFAULT_DOCK_Y,
            cabinet_direction="bad-direction",
            safe_distance_front=2500,
            safe_half_width=1200,
            close_margin=300,
        )
    except ValueError as err:
        assert "cabinet_direction" in str(err)
    else:
        raise AssertionError("Expected ValueError for invalid direction")
