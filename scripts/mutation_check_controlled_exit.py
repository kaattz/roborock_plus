"""Confirm the controlled-exit tests catch the defects that shipped.

Two rounds of real failures are pinned here:

* 2026-11-08, first live run: the arrival check read a field the observation does
  not define, so every exit died on its deadline and left the door open with no
  alarm. Nothing failed, because nothing tested the trip.
* 2026-11-08, same day: the exit was reworked so the waypoint is a direction plus
  a distance instead of two raw coordinates. The obvious direction rule -- head
  for the nearest edge -- is wrong for this house, by 900mm.

Each mutation puts one piece of the old (or tempting) behaviour back and requires
the suite to go red.

The baseline and mutations run in a disposable copy. Production checkout files
are only read, including when a subprocess fails, times out or is interrupted.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPONENT = REPO / "custom_components" / "roborock_plus"
RUNTIME = COMPONENT / "garage_runtime.py"
DOOR = COMPONENT / "garage_door.py"
CONFIG = COMPONENT / "garage_config.py"
TESTS = ["tests/test_controlled_exit_runtime.py", "tests/test_exit_geometry.py",
         "tests/test_garage_ownership_release.py"]
#: The package namespace builder the tests import; without it collection fails in
#: a copy that has only the test files.
TEST_HELPERS = ["tests/garage_pkg_loader.py"]
TIMEOUT_SECONDS = 180


def _three(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    return runtime, door, config


# --- the 2026-11-08 incident -------------------------------------------------

def read_the_missing_attribute_again(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """THE INCIDENT: read `robot.position`, which the record does not define.

    `getattr(..., None)` then yields None, the distance comparison is never
    satisfied, and the loop can only time out -- exactly what happened.
    """
    return runtime.replace(
        "            if self.controller.controlled_exit_arrived(target):\n",
        "            position = getattr(self.controller.robot, 'position', None)\n"
        "            x = getattr(position, 'x', None)\n"
        "            y = getattr(position, 'y', None)\n"
        "            if x is not None and y is not None and math.hypot(\n"
        "                    x - target[0], y - target[1]) <= 200:\n",
    ), door, config


def drop_the_position_from_the_observation(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """Keep every call site but stop carrying the coordinate they read."""
    return runtime.replace("            x=position_x, y=position_y,\n", ""), door, config


def trust_the_distance_alone(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """Accept a coordinate hit without first proving the robot left the zone."""
    return runtime, door.replace(
        "        if not self.robot_outside():\n"
        "            return False\n"
        "        return self.trip_finished()\n",
        "        return self.trip_finished()\n",
    ), config


def forget_the_settle(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """Act on a single sample.

    Measured 10:47:00: the robot reported outside three seconds into the trip,
    still driving. One sample is not an arrival.
    """
    return runtime.replace(
        "            if self.controller.controlled_exit_arrived(target):\n"
        "                agreeing.add(self.controller.robot.sample_id)\n"
        "                if len(agreeing) >= self.CONTROLLED_EXIT_SETTLE_SAMPLES:\n"
        "                    self._report_arrival_offset(target)\n"
        "                    return\n",
        "            if self.controller.controlled_exit_arrived(target):\n"
        "                self._report_arrival_offset(target)\n"
        "                return\n",
    ), door, config


def let_a_failed_exit_vanish(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """THE SECOND HALF OF THE INCIDENT: raise, and settle nothing."""
    return runtime.replace(
        "            await self._abandon_controlled_exit(err)\n"
        "            raise\n",
        "            raise\n",
    ), door, config


def leave_the_sequence_flag_set(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """Tidy up the door but keep claiming a clean owns it."""
    return runtime.replace(
        "            self.controller.task_in_progress = False\n"
        "            self.controller.last_error = reported\n",
        "            self.controller.last_error = reported\n",
    ), door, config


def swallow_the_failure_event(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """Settle the state but tell nobody."""
    return runtime.replace("            self.notify(reported)\n", ""), door, config


def reopen_the_door_after_the_exit(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """THE SECOND DEFECT: reopen the door the exit just closed.

    v0.9.0's spec (REQ-39) says a robot that is already outside does not need
    passage. The v0.10.0 rewrite lost that branch, so a successful exit confirmed
    the door shut and then opened it again before the clean.
    """
    return runtime.replace(
        "        exited = False\n"
        "        if name in _CONTROLLED_EXIT_COMMANDS and self._should_controlled_exit(name):\n",
        "        exited = False\n"
        "        if False:\n",
    ), door, config


# --- the 0.12.0 geometry rework ----------------------------------------------

def head_for_the_nearest_edge(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """THE GEOMETRY TRAP: derive the exit direction from the nearest edge.

    Measured 2026-11-08: the dock is 373mm from +X and 325mm from +Y, both nearer
    than the 1316mm -Y edge the robot actually uses. This rule answers +Y, which
    points the robot at the closed door beside the cabinet -- the 2026-10-25 graze
    the feature exists to avoid.
    """
    return runtime, door, config.replace(
        "    dock_x, dock_y = dock\n"
        "    vy = (zone_min[1] + zone_max[1]) / 2 - dock_y\n",
        "    dock_x, dock_y = dock\n"
        "    candidates = {\n"
        "        'west': dock_x - zone_min[0], 'east': zone_max[0] - dock_x,\n"
        "        'north': dock_y - zone_min[1], 'south': zone_max[1] - dock_y,\n"
        "    }\n"
        "    return min(candidates, key=candidates.get)\n"
        "    vy = (zone_min[1] + zone_max[1]) / 2 - dock_y\n",
    )


def ignore_the_manual_override(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """Let `auto` win even when the operator picked an axis on purpose."""
    return runtime, door, config.replace(
        "    if requested in EXIT_AXES:\n"
        "        return requested\n",
        "    if False:\n"
        "        return requested\n",
    )


def migrate_without_noticing_the_offset(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """Convert a sideways waypoint and stay quiet about the lost component.

    The new model cannot express a lateral offset. Dropping it silently means an
    operator whose waypoint was mostly sideways gets a different path with no
    indication that anything changed.
    """
    return runtime.replace(
        "        if lateral > CONTROLLED_EXIT_LATERAL_WARN_MM:\n"
        "            _LOGGER.warning(\n"
        "                'Garage exit waypoint for %s sits %.0f mm off the exit axis; the '\n"
        "                'lateral component cannot be represented and is dropped',\n"
        "                self.coordinator.duid, lateral)\n",
        "",
    ), door, config


def keep_the_old_tolerance(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """Restore the 400mm tolerance, leaving only 223mm of clearance at worst."""
    return runtime, door, config.replace(
        "CONTROLLED_EXIT_TOLERANCE_MM = 200", "CONTROLLED_EXIT_TOLERANCE_MM = 400")


def build_the_target_from_coordinates(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """Skip the derived waypoint and read the legacy coordinates instead.

    That is the pre-0.12.0 behaviour. It must be impossible for it to come back
    silently, because a binding that has migrated no longer carries coordinates --
    this would die with a KeyError rather than drive somewhere wrong.
    """
    return runtime.replace(
        "        distance = self._controlled_exit_distance()\n"
        "        geometry = self._exit_geometry()\n"
        "        if distance is None or geometry is None:\n"
        "            raise GarageControlError('exit_target_unavailable')\n",
        "        return {'x': int(self.binding['controlled_exit_x']),\n"
        "                'y': int(self.binding['controlled_exit_y']),\n"
        "                'direction': 'north',\n"
        "                'distance_mm': 0,\n"
        "                'timeout': float(self.binding['controlled_exit_timeout_seconds'])}\n"
        "        distance = self._controlled_exit_distance()\n"
        "        geometry = self._exit_geometry()\n"
        "        if distance is None or geometry is None:\n"
        "            raise GarageControlError('exit_target_unavailable')\n",
    ), door, config


def keep_ownership_after_shutdown(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """THE RELOAD TRAP: leave the door in `owners` after shutdown.

    `async_setup_garages` refuses a cover that is already owned, so a reload finds
    the door held by a runtime that no longer exists, reports `duplicate_binding`,
    never attaches, and refuses every clean command afterwards -- the robot beeps
    and nothing happens.
    """
    return runtime.replace(
        "        owners = getattr(self.hass, 'data', {}).get(REGISTRY_KEY)\n"
        "        if isinstance(owners, dict):\n"
        "            cover = self.binding.get('cover_entity_id')\n"
        "            if cover is not None and owners.get(cover) is self:\n"
        "                del owners[cover]\n",
        "",
    ), door, config


def let_the_tolerance_decide_arrival(runtime: str, door: str, config: str) -> tuple[str, str, str]:
    """THE 100mm TRAP: judge arrival by distance, ignoring whether the trip ended.

    That is the order this shipped with before the rework. Whenever the tolerance
    exceeds the configured distance the coordinate branch fires the moment the
    robot clears the zone edge, so the distance is never travelled -- a 100mm
    waypoint with a 200mm tolerance means "stop as soon as you are outside".
    """
    return runtime, door.replace(
        "        if not self.robot_outside():\n"
        "            return False\n"
        "        return self.trip_finished()\n",
        "        if not self.robot_outside():\n"
        "            return False\n"
        "        position = self.contained_position()\n"
        "        if position is not None:\n"
        "            from .garage_config import CONTROLLED_EXIT_TOLERANCE_MM\n"
        "            if math.hypot(position[0] - target[0], position[1] - target[1]) <= CONTROLLED_EXIT_TOLERANCE_MM:\n"
        "                return True\n"
        "        return self.trip_finished()\n",
    ), config


MUTATIONS = [
    ("THE INCIDENT: the arrival check reads a field that does not exist",
     read_the_missing_attribute_again),
    ("the observation stops carrying the coordinate", drop_the_position_from_the_observation),
    ("the distance is trusted without proving the robot left the zone",
     trust_the_distance_alone),
    ("a single sample is treated as arrival", forget_the_settle),
    ("a failed exit raises and settles nothing", let_a_failed_exit_vanish),
    ("a failed exit leaves the sequence flag set", leave_the_sequence_flag_set),
    ("a failed exit fires no garage_issue event", swallow_the_failure_event),
    ("the door is reopened after a successful exit", reopen_the_door_after_the_exit),
    ("THE GEOMETRY TRAP: the exit direction follows the nearest edge",
     head_for_the_nearest_edge),
    ("a manual direction is ignored in favour of auto", ignore_the_manual_override),
    ("a sideways legacy waypoint migrates without a warning",
     migrate_without_noticing_the_offset),
    ("the arrival tolerance goes back to 400mm", keep_the_old_tolerance),
    ("the waypoint is read from coordinates instead of derived",
     build_the_target_from_coordinates),
    ("shutdown keeps the door owned, so a reload can never take it back",
     keep_ownership_after_shutdown),
    ("THE 100mm TRAP: the tolerance decides arrival before the trip does",
     let_the_tolerance_decide_arrival),
]


def _run_tests(workspace: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-q", "-p", "no:cacheprovider"],
        cwd=workspace, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
    )


def main() -> int:
    originals = {
        path: path.read_text(encoding="utf-8")
        for path in (RUNTIME, DOOR, CONFIG)
    }
    caught = 0
    escaped: list[str] = []

    with tempfile.TemporaryDirectory(prefix="roborock-controlled-exit-") as directory:
        workspace = Path(directory)
        component = workspace / "custom_components" / "roborock_plus"
        component.mkdir(parents=True)
        (workspace / "tests").mkdir()
        for source in COMPONENT.glob("*.py"):
            shutil.copyfile(source, component / source.name)
        for test in TESTS + TEST_HELPERS:
            shutil.copyfile(REPO / test, workspace / test)
        targets = {
            RUNTIME: component / RUNTIME.name,
            DOOR: component / DOOR.name,
            CONFIG: component / CONFIG.name,
        }
        for source, copy in targets.items():
            copy.write_text(originals[source], encoding="utf-8")

        try:
            baseline = _run_tests(workspace)
        except subprocess.TimeoutExpired:
            print("BASELINE FAILED (timed out)")
            return 1
        if baseline.returncode != 0:
            print("BASELINE FAILED")
            print(baseline.stdout + baseline.stderr)
            return 1
        print("BASELINE PASSED", flush=True)

        for name, mutate in MUTATIONS:
            runtime, door, config = mutate(
                originals[RUNTIME], originals[DOOR], originals[CONFIG])
            unchanged = (
                runtime == originals[RUNTIME]
                and door == originals[DOOR]
                and config == originals[CONFIG]
            )
            if unchanged:
                print(f"SETUP FAILED  {name}  (anchor not found)")
                escaped.append(name)
                continue
            values = {RUNTIME: runtime, DOOR: door, CONFIG: config}
            try:
                for source, copy in targets.items():
                    compile(values[source], str(copy), "exec")
                    copy.write_text(values[source], encoding="utf-8")
            except SyntaxError as err:
                print(f"SETUP FAILED  {name}  (mutation does not compile: {err})")
                escaped.append(name)
                continue
            try:
                result = _run_tests(workspace)
            except subprocess.TimeoutExpired:
                print(f"CAUGHT        {name}  (timed out)", flush=True)
                caught += 1
                continue
            if result.returncode != 0:
                print(f"CAUGHT        {name}", flush=True)
                caught += 1
            else:
                print(f"ESCAPED       {name}", flush=True)
                escaped.append(name)

        for source, copy in targets.items():
            copy.write_text(originals[source], encoding="utf-8")

    print()
    print(f"{caught}/{len(MUTATIONS)} mutations caught")
    if escaped:
        print("ESCAPED: " + "; ".join(escaped))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
