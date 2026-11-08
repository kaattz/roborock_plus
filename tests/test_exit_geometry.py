"""Geometry behind the controlled-exit waypoint: direction, distance, target.

The waypoint used to be two raw map coordinates. Nobody could look at 26500 and
tell which way the robot would travel or how much clearance it had, and those are
the two things that decide whether shutting the door is safe.

The household's numbers, measured 2026-10-25 and again 2026-11-08:

    dock                  (25696, 28439)
    danger zone           x 24997-26069, y 27123-28764
    real exit direction   -Y
    dock to -Y edge       1316 mm
    current waypoint      (25696, 26500)  = 623 mm past that edge

These tests pin the rule that reproduces those numbers, because the obvious rule
("head for the nearest edge") does not: the dock is 373mm from +X and 325mm from
+Y, both nearer than the 1316mm -Y edge it actually uses.
"""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

import garage_pkg_loader  # noqa: F401 - builds the package namespace

PATH = Path(__file__).resolve().parents[1] / "custom_components/roborock_plus/garage_config.py"


def _load():
    """Load the pure config module directly; it has no Home Assistant imports."""
    spec = spec_from_file_location("garage_config_exit_geometry", PATH)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config = _load()
CONTROLLED_EXIT_LATERAL_WARN_MM = config.CONTROLLED_EXIT_LATERAL_WARN_MM
EXIT_AXES = config.EXIT_AXES
EXIT_DIRECTIONS = config.EXIT_DIRECTIONS
GarageConfigError = config.GarageConfigError
exit_distance_from_dock = config.exit_distance_from_dock
legacy_exit_distance = config.legacy_exit_distance
resolve_exit_direction = config.resolve_exit_direction
resolve_exit_target = config.resolve_exit_target

DOCK = (25696.0, 28439.0)
ZONE_MIN = (24997.0, 27123.0)
ZONE_MAX = (26069.0, 28764.0)


def target(direction: str, distance: float) -> tuple[int, int] | None:
    return resolve_exit_target(
        dock=DOCK, zone_min=ZONE_MIN, zone_max=ZONE_MAX,
        direction=direction, distance_mm=distance,
    )


def direction(requested: str = "auto") -> str | None:
    return resolve_exit_direction(
        dock=DOCK, zone_min=ZONE_MIN, zone_max=ZONE_MAX, requested=requested,
    )


# --- the rule that matters ---------------------------------------------------

def test_auto_picks_the_measured_exit_direction():
    """-Y, and emphatically not the nearest edge.

    Nearest-edge would answer +X (373mm) or +Y (325mm). Both drive the robot at
    the closed door beside the cabinet.
    """
    assert direction() == "north"


def test_the_current_waypoint_is_reproduced_exactly():
    """623mm past the edge is the waypoint the household is running today.

    If this fails, the direction or the distance convention is wrong, and any
    migration would silently move the robot somewhere else.
    """
    assert target("north", 623) == (25696, 26500)


def test_the_dock_relative_figure_is_the_intuitive_one():
    """1316mm of zone plus 623mm of clearance is "1939mm forward"."""
    assert exit_distance_from_dock(
        dock=DOCK, zone_min=ZONE_MIN, zone_max=ZONE_MAX,
        direction="auto", distance_mm=623,
    ) == pytest.approx(1939.0)


# --- all four axes -----------------------------------------------------------

@pytest.mark.parametrize("axis,expected", [
    ("north", (25696, 26500)),
    ("south", (25696, 29387)),
    ("west", (24374, 28439)),
    ("east", (26692, 28439)),
])
def test_each_axis_clears_its_own_edge_by_the_stated_distance(axis, expected):
    assert target(axis, 623) == expected


@pytest.mark.parametrize("axis", EXIT_AXES)
def test_the_cross_axis_coordinate_stays_on_the_dock(axis):
    """The robot drives straight out; it does not drift sideways."""
    dock_x, dock_y = DOCK
    x, y = target(axis, 500)
    if axis in ("north", "south"):
        assert x == dock_x
    else:
        assert y == dock_y


@pytest.mark.parametrize("axis", EXIT_AXES)
def test_the_result_really_is_outside_the_zone(axis):
    """A construction that lands inside would close the door on the robot."""
    x, y = target(axis, 1)
    assert not (ZONE_MIN[0] <= x <= ZONE_MAX[0] and ZONE_MIN[1] <= y <= ZONE_MAX[1])


# --- manual override ---------------------------------------------------------

def test_a_manual_direction_wins_over_auto():
    """Auto says north; the operator overrode to west and gets west."""
    assert direction("auto") == "north"
    assert direction("west") == "west"
    assert target("west", 623) != target("north", 623)


def test_auto_is_the_default_and_is_resolved_before_use():
    assert "auto" in EXIT_DIRECTIONS
    assert "auto" not in EXIT_AXES
    assert direction("auto") in EXIT_AXES


def test_an_unknown_direction_is_refused():
    with pytest.raises(GarageConfigError) as err:
        direction("up")
    assert err.value.reason == "invalid_controlled_exit_direction"


# --- degenerate geometry -----------------------------------------------------

def test_a_dock_at_the_zone_centre_cannot_answer():
    """No direction is implied, so the caller must refuse rather than invent one."""
    centre = ((ZONE_MIN[0] + ZONE_MAX[0]) / 2, (ZONE_MIN[1] + ZONE_MAX[1]) / 2)
    assert resolve_exit_direction(
        dock=centre, zone_min=ZONE_MIN, zone_max=ZONE_MAX, requested="auto",
    ) is None
    assert resolve_exit_target(
        dock=centre, zone_min=ZONE_MIN, zone_max=ZONE_MAX,
        direction="auto", distance_mm=623,
    ) is None


def test_a_tie_between_axes_resolves_to_the_vertical_one():
    """|vy| == |vx| has to answer something deterministic."""
    dock = (ZONE_MIN[0] + 1000.0, ZONE_MIN[1] + 1000.0)   # equal dx, dy to centre
    assert resolve_exit_direction(
        dock=dock, zone_min=ZONE_MIN, zone_max=ZONE_MAX, requested="auto",
    ) in EXIT_AXES


# --- the warning threshold ---------------------------------------------------

def test_the_lateral_threshold_is_one_body_width():
    """Migration warns past this, because a sideways legacy point loses its offset."""
    assert CONTROLLED_EXIT_LATERAL_WARN_MM == 1000


# --- the arrival tolerance ---------------------------------------------------

def test_the_tolerance_keeps_the_robot_well_clear_of_the_edge():
    """623mm of clearance minus the tolerance is the worst case that can happen.

    At 400mm it was 223mm -- close enough that a door swing could reach the
    robot. At 200mm it is 423mm. Asserting the constant alone would pass for any
    value; asserting the clearance is what makes the number mean something.
    """
    assert config.CONTROLLED_EXIT_TOLERANCE_MM == 200
    worst_case = 623 - config.CONTROLLED_EXIT_TOLERANCE_MM
    assert worst_case >= 400, (
        f'worst-case clearance {worst_case}mm is too close to the door')


def test_arrival_is_decided_by_the_trip_not_by_the_tolerance():
    """THE 100mm CASE: a coordinate near the waypoint is not arrival.

    100mm waypoint, 200mm tolerance. A robot that has just cleared the zone edge
    sits ~77mm from the waypoint -- inside the tolerance. Judging by distance
    first calls that arrived, so the robot never travels the distance it was
    configured for and the setting quietly does nothing.

    The trip ending is what decides. ``robot_outside()`` still gates it against
    the robot's *current* position, so a robot inside the zone is never arrived
    however its state reads.
    """
    from types import SimpleNamespace as NS

    from _garage_pkg.garage_door import GarageController

    def controller(*, state, task_active, y, outside=True):
        c = GarageController(
            binding={"open_source": "contact", "close_source": "contact",
                     "open_contact_state": "off", "close_contact_state": "on",
                     "contact_settle_seconds": 0, "operation_timeout_seconds": 5},
            door_observation=lambda: {
                "cover_available": True, "position": 100,
                "open_contact": "off", "close_contact": "on",
            },
            robot_observation=lambda: NS(
                state=state, docked=False, outside_danger_zone=outside,
                position_trusted=True, fresh=True, task_active=task_active,
                x=25656.0, y=y,
            ),
            move_door=lambda direction: None,
            close_allowed=lambda: True,
        )
        c.refresh()
        return c

    target = (25656.0, 27023.0)          # the configured 100mm point

    # Just outside the edge and still driving: 77mm away, inside the tolerance,
    # and still not an arrival.
    just_out = controller(state="going_to_target", task_active=True, y=27100.0)
    assert just_out.controlled_exit_arrival_offset(target) == pytest.approx(77.0)
    assert just_out.controlled_exit_arrived(target) is False

    # Concentric with the waypoint but still driving: still not an arrival.
    at_point_driving = controller(state="going_to_target", task_active=True, y=27023.0)
    assert at_point_driving.controlled_exit_arrived(target) is False

    # Trip over, outside: arrived.
    finished = controller(state="idle", task_active=False, y=27023.0)
    assert finished.controlled_exit_arrived(target) is True

    # Trip over, but the robot is inside the zone: never an arrival.
    inside = controller(state="idle", task_active=False, y=27200.0, outside=False)
    assert inside.controlled_exit_arrived(target) is False, (
        "the door must never be closed on a robot still inside the zone")


def test_arrival_offset_reports_zero_when_the_robot_hits_the_waypoint():
    from types import SimpleNamespace as NS

    from _garage_pkg.garage_door import GarageController

    c = GarageController(
        binding={"open_source": "contact", "close_source": "contact",
                 "open_contact_state": "off", "close_contact_state": "on",
                 "contact_settle_seconds": 0, "operation_timeout_seconds": 5},
        door_observation=lambda: {
            "cover_available": True, "position": 100,
            "open_contact": "off", "close_contact": "on",
        },
        robot_observation=lambda: NS(
            state="idle", docked=False, outside_danger_zone=True,
            position_trusted=True, fresh=True, task_active=False,
            x=25656.0, y=27023.0,
        ),
        move_door=lambda direction: None,
        close_allowed=lambda: True,
    )
    c.refresh()
    assert c.controlled_exit_arrival_offset((25656.0, 27023.0)) == pytest.approx(0.0)


def test_an_untrusted_position_reports_no_offset_rather_than_zero():
    from types import SimpleNamespace as NS

    from _garage_pkg.garage_door import GarageController

    c = GarageController(
        binding={"open_source": "contact", "close_source": "contact",
                 "open_contact_state": "off", "close_contact_state": "on",
                 "contact_settle_seconds": 0, "operation_timeout_seconds": 5},
        door_observation=lambda: {
            "cover_available": True, "position": 100,
            "open_contact": "off", "close_contact": "on",
        },
        robot_observation=lambda: NS(
            state="idle", docked=False, outside_danger_zone=True,
            position_trusted=False, fresh=True, task_active=False,
            x=25656.0, y=27023.0,
        ),
        move_door=lambda direction: None,
        close_allowed=lambda: True,
    )
    c.refresh()
    assert c.controlled_exit_arrival_offset((25656.0, 27023.0)) is None


# --- migration from the coordinate model -------------------------------------

def test_the_installed_waypoint_migrates_without_moving_the_robot():
    """The household's live binding is (25696, 26500); it must become 623.

    Converting and re-deriving has to land on the same point, or upgrading would
    silently drive the robot somewhere it has never been.
    """
    distance, lateral = legacy_exit_distance(
        waypoint=(25696.0, 26500.0), dock=DOCK,
        zone_min=ZONE_MIN, zone_max=ZONE_MAX, direction="auto",
    )
    assert distance == 623
    assert lateral == pytest.approx(0.0)
    assert target("north", distance) == (25696, 26500)


@pytest.mark.parametrize("waypoint,axis,expected", [
    ((25696.0, 26500.0), "north", 623),
    ((25696.0, 29387.0), "south", 623),
    ((24374.0, 28439.0), "west", 623),
    ((26692.0, 28439.0), "east", 623),
])
def test_each_axis_round_trips(waypoint, axis, expected):
    distance, _ = legacy_exit_distance(
        waypoint=waypoint, dock=DOCK, zone_min=ZONE_MIN, zone_max=ZONE_MAX,
        direction=axis,
    )
    assert distance == expected
    assert target(axis, distance) == (round(waypoint[0]), round(waypoint[1]))


def test_a_sideways_legacy_waypoint_reports_its_offset():
    """The lateral component cannot be represented; the caller needs the size.

    Warned about rather than refused: this is an existing working configuration,
    and refusing would block the upgrade. But it must be measurable, because the
    path will change.
    """
    _, lateral = legacy_exit_distance(
        waypoint=(25800.0, 26500.0), dock=DOCK,
        zone_min=ZONE_MIN, zone_max=ZONE_MAX, direction="north",
    )
    assert lateral == pytest.approx(104.0)


def test_a_waypoint_inside_the_zone_is_refused_not_invented():
    """Such a waypoint could never have worked; inventing a distance hides that."""
    with pytest.raises(GarageConfigError) as err:
        legacy_exit_distance(
            waypoint=(25696.0, 28000.0), dock=DOCK,
            zone_min=ZONE_MIN, zone_max=ZONE_MAX, direction="north",
        )
    assert err.value.reason == "controlled_exit_target_inside_zone"
