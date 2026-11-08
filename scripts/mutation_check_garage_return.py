"""Confirm the return-trip tests catch the defects that shipped.

Two real failures are pinned here, both observed on 2026-11-08:

* 14:33:16 -- the door was shut on a robot driving home. `update_returning()` left
  the phase as `outside` (the value that means "a clean is running beyond the
  door"), and the departure rule closed on it. The robot was then stuck outside.
* 14:33:50 -- a pause with no resume. The close did not go through and the release
  that should have followed was not verified, so the robot stood still.

The same shape bites mid-task: a robot that returns to charge, empty or wash
during a clean reports `returning_home` while `task_active` is still on. Excluding
only `is_returning()` would leave that case closing the door on a robot that is
about to drive out again.

Each mutation reverts one guard and requires the suite to go red.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPONENT = REPO / "custom_components" / "roborock_plus"
DOOR = COMPONENT / "garage_door.py"
RUNTIME = COMPONENT / "garage_runtime.py"
TESTS = ["tests/test_garage_return_trip.py", "tests/test_garage_charge_command.py",
         "tests/test_garage_door_rules.py"]
TEST_HELPERS = ["tests/garage_pkg_loader.py"]
TIMEOUT_SECONDS = 120


def drop_the_way_back_guard(door: str) -> str:
    """THE 14:33:16 BUG: no "on the way back" exclusion at all.

    A returning robot reports outside the zone for the whole doorway approach, so
    the departure rule shuts the door on it.
    """
    return door.replace(
        "        if self.is_on_the_way_back():\n"
        "            # The robot is driving in, or is home with work still to do. Whatever\n"
        "            # its position says, this is not \"the clean went out and the door can\n"
        "            # be shut\" -- that mistake closed the door on a returning robot at\n"
        "            # 14:33:16 on 2026-11-08 and left it stuck outside.\n"
        "            return False\n",
        "",
    )


def ignore_mid_task_dock_visits(door: str) -> str:
    """Exclude only `is_returning()`, missing charging/emptying/washing mid-clean.

    The robot is docked with `task_active` on and will drive out again by itself;
    closing now puts a shut door in its path.
    """
    return door.replace(
        "        if self.is_returning():\n            return True\n"
        "        return bool(self.robot_docked() and self.task_running())\n",
        "        return self.is_returning()\n",
    )


def let_the_return_phase_look_like_an_exit(door: str) -> str:
    """Put `phase = 'outside'` back at the end of a return.

    That value is what the departure rule closes on, so the next tick shuts the
    door on a robot that has just been let through it.
    """
    return door.replace(
        "        self.phase = 'resuming_return'\n"
        "        await resume()\n"
        "        # Deliberately not 'outside'. That value means \"a clean is running beyond\n"
        "        # the door\", and the departure rule closes on it -- which is how the door\n"
        "        # was shut on a robot driving home at 14:33:16 on 2026-11-08. The return\n"
        "        # stays in its own phase until the robot is docked.\n"
        "        self.phase = 'resuming_return'\n",
        "        self.phase = 'resuming_return'\n"
        "        await resume()\n"
        "        self.phase = 'outside'\n",
    )


def swallow_the_release_failure(door: str) -> str:
    """Report nothing when the release itself fails.

    The robot stays paused and the phase claims the door work finished, which is
    how it stood still from 14:33:50 onward.
    """
    return door.replace(
        "            try:\n"
        "                await resume()\n"
        "            except Exception as err:  # noqa: BLE001 - reported, never swallowed\n"
        "                reason = getattr(err, 'reason', None) or 'resume_failed'\n"
        "                _LOGGER.error(\n"
        "                    'Garage could not release the paused robot (%s) after an exit '\n"
        "                    'attempt that %s; it is still paused',\n"
        "                    reason,\n"
        "                    'closed the door' if closed else f'failed to close ({close_error})',\n"
        "                )\n"
        "                self.last_error = reason\n"
        "                self.phase = 'blocked'\n",
        "            await resume()\n",
    )


def call_a_closed_door_a_success(door: str) -> str:
    """Treat "the door shut" as success even when the robot was never released."""
    return door.replace(
        "        if closed and not release_failed:\n",
        "        if closed:\n",
    )


def forward_charge_untouched(runtime: str) -> str:
    """THE SHUT-DOOR CASE: send `app_charge` without opening the door first.

    A clean is running, the departure rule has shut the door, and the household
    presses "return to base". The robot drives at a closed door.
    """
    return runtime.replace(
        "        if name == 'app_charge':\n"
        "            return await self._send_charge(command, send)\n",
        "        if name == 'app_charge':\n"
        "            return await send()\n",
    )


def charge_without_pausing(runtime: str) -> str:
    """Open the door, but let the robot keep moving while it does."""
    return runtime.replace(
        "            await self._pause_robot()\n"
        "            self.controller.refresh()\n",
        "            self.controller.refresh()\n",
    )


def charge_even_when_the_door_will_not_open(runtime: str) -> str:
    """Swallow a failed open and send the command anyway."""
    return runtime.replace(
        "        except GarageControlError:\n"
        "            self.publish()\n"
        "            raise\n",
        "        except GarageControlError:\n"
        "            self.publish()\n",
    )


def open_the_door_for_every_charge(runtime: str) -> str:
    """Open the door even for a docked robot.

    An unasked door movement is its own defect: a robot already inside needs no
    passage, and the household never asked for the door to move.
    """
    return runtime.replace(
        "        if self.controller.robot_docked():\n"
        "            return False\n"
        "        if not self.controller.task_running():\n"
        "            return False\n",
        "        if False:\n"
        "            return False\n",
    )


def resume_without_a_confirmed_pause(door: str) -> str:
    """THE COLLISION: release the robot even though the stop was never confirmed.

    The robot may still be driving, and the door did not open. Sending it forward
    is the one thing the whole sequence exists to prevent.
    """
    return door.replace(
        "        if not await self._confirm_pause():\n"
        "            # Not confirmed stopped. The door is not touched and the robot is NOT\n"
        "            # resumed: an unconfirmed stop may still be a moving robot, and a\n"
        "            # resume would send it at a door that never opened. It waits for a\n"
        "            # human, which is inconvenient and safe.\n"
        "            self.phase = 'blocked'\n"
        "            self.last_error = 'pause_not_observed'\n"
        "            raise GarageControlError('pause_not_observed')\n",
        "        if not await self._confirm_pause():\n"
        "            self.phase = 'blocked'\n"
        "            self.last_error = 'pause_not_observed'\n"
        "            await resume()\n"
        "            raise GarageControlError('pause_not_observed')\n",
    )


def open_the_door_without_a_confirmed_pause(door: str) -> str:
    """Move the door while the robot may still be driving at it."""
    return door.replace(
        "        if not await self._confirm_pause():\n"
        "            # Not confirmed stopped. The door is not touched and the robot is NOT\n"
        "            # resumed: an unconfirmed stop may still be a moving robot, and a\n"
        "            # resume would send it at a door that never opened. It waits for a\n"
        "            # human, which is inconvenient and safe.\n"
        "            self.phase = 'blocked'\n"
        "            self.last_error = 'pause_not_observed'\n"
        "            raise GarageControlError('pause_not_observed')\n"
        "        self.phase = 'opening'\n",
        "        self.phase = 'opening'\n",
    )


def read_the_pause_once_instead_of_waiting(door: str) -> str:
    """THE 15:19:49 BUG: a single lagging sample decides the pause failed.

    The command went out, the robot stopped, and the sample in hand was the one
    taken before it. The sequence then gave up with the door shut and the robot
    paused outside it.
    """
    return door.replace(
        "        deadline = self.clock() + self.PAUSE_CONFIRM_TIMEOUT_SECONDS\n"
        "        for _ in range(self.PAUSE_CONFIRM_MAX_READS):\n"
        "            self.refresh()\n"
        "            if self.robot_state() == 'paused':\n"
        "                return True\n"
        "            if self.clock() >= deadline:\n"
        "                return False\n"
        "            await self.sleep(self.PAUSE_CONFIRM_POLL_SECONDS)\n"
        "        return False\n",
        "        self.refresh()\n"
        "        return self.robot_state() == 'paused'\n",
    )


def trust_the_snapshot_instead_of_the_latch(door: str) -> str:
    """THE 16:16 LOOP: judge "on the way back" by the current state only.

    A return is `returning_home -> paused -> returning_home -> docking`, and
    `paused` is not a returning state. Between the pause this integration commands
    and the resume, the snapshot says "not returning", so the departure rule shut
    the door it had just opened -- 1.4 seconds after the open was confirmed -- and
    the cycle repeated.
    """
    return door.replace(
        "        if self.return_in_progress and not self.return_completed:\n"
        "            # The latch outranks the snapshot, but not reality: a robot confirmed on\n"
        "            # its dock is home, so the return it was in cannot still be in progress.\n"
        "            if self.robot_docked():\n"
        "                self.note_return_completed()\n"
        "            else:\n"
        "                return True\n",
        "",
    )


def never_release_the_latch(door: str) -> str:
    """Release the latch only in `sequence_finished`, never on a docked reading.

    A door that can never be shut is its own failure: the clean ends and the door
    stays open for hours.
    """
    return door.replace(
        "            if self.robot_docked():\n"
        "                self.note_return_completed()\n"
        "            else:\n"
        "                return True\n",
        "            return True\n",
    )


MUTATIONS = [
    ("THE 14:33:16 BUG: no on-the-way-back guard at all", drop_the_way_back_guard),
    ("only returning_home is excluded, not a mid-task dock visit",
     ignore_mid_task_dock_visits),
    ("a return leaves the phase looking like a departed clean",
     let_the_return_phase_look_like_an_exit),
    ("THE 14:33:50 BUG: a failed release is swallowed", swallow_the_release_failure),
    ("a closed door is called success even when the robot is still paused",
     call_a_closed_door_a_success),
    ("THE SHUT-DOOR CASE: app_charge is forwarded without opening the door",
     forward_charge_untouched),
    ("the robot is not paused before the door opens", charge_without_pausing),
    ("a door that will not open does not stop the command",
     charge_even_when_the_door_will_not_open),
    ("the door is opened even for a docked robot", open_the_door_for_every_charge),
    ("THE COLLISION: resumed without a confirmed pause",
     resume_without_a_confirmed_pause),
    ("the door moves without a confirmed pause",
     open_the_door_without_a_confirmed_pause),
    ("THE 15:19:49 BUG: the pause is read once instead of waited for",
     read_the_pause_once_instead_of_waiting),
    ("THE 16:16 LOOP: the latch is ignored, the snapshot decides",
     trust_the_snapshot_instead_of_the_latch),
    ("the latch is never released, so the door can never shut",
     never_release_the_latch),
]


def _run_tests(workspace: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-q", "-p", "no:cacheprovider"],
        cwd=workspace, capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
    )


def main() -> int:
    original = DOOR.read_text(encoding="utf-8")
    runtime_original = RUNTIME.read_text(encoding="utf-8")
    caught = 0
    escaped: list[str] = []

    with tempfile.TemporaryDirectory(prefix="roborock-return-trip-") as directory:
        workspace = Path(directory)
        component = workspace / "custom_components" / "roborock_plus"
        component.mkdir(parents=True)
        (workspace / "tests").mkdir()
        for source in COMPONENT.glob("*.py"):
            shutil.copyfile(source, component / source.name)
        for test in TESTS + TEST_HELPERS:
            shutil.copyfile(REPO / test, workspace / test)
        door_path = component / "garage_door.py"
        runtime_path = component / "garage_runtime.py"
        door_path.write_text(original, encoding="utf-8")
        runtime_path.write_text(runtime_original, encoding="utf-8")

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
            if mutate.__code__.co_varnames[:1] == ("runtime",):
                mutated_runtime = mutate(runtime_original)
                if mutated_runtime == runtime_original:
                    print(f"SETUP FAILED  {name}  (anchor not found)")
                    escaped.append(name)
                    continue
                try:
                    compile(mutated_runtime, str(runtime_path), "exec")
                except SyntaxError as err:
                    print(f"SETUP FAILED  {name}  (does not compile: {err})")
                    escaped.append(name)
                    continue
                runtime_path.write_text(mutated_runtime, encoding="utf-8")
                try:
                    result = _run_tests(workspace)
                except subprocess.TimeoutExpired:
                    print(f"CAUGHT        {name}  (timed out)", flush=True)
                    caught += 1
                    continue
                finally:
                    runtime_path.write_text(runtime_original, encoding="utf-8")
                if result.returncode != 0:
                    print(f"CAUGHT        {name}", flush=True)
                    caught += 1
                else:
                    print(f"ESCAPED       {name}", flush=True)
                    escaped.append(name)
                continue
            mutated = mutate(original)
            if mutated == original:
                print(f"SETUP FAILED  {name}  (anchor not found)")
                escaped.append(name)
                continue
            try:
                compile(mutated, str(door_path), "exec")
            except SyntaxError as err:
                print(f"SETUP FAILED  {name}  (does not compile: {err})")
                escaped.append(name)
                continue
            door_path.write_text(mutated, encoding="utf-8")
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

        door_path.write_text(original, encoding="utf-8")

    print()
    print(f"{caught}/{len(MUTATIONS)} mutations caught")
    if escaped:
        print("ESCAPED: " + "; ".join(escaped))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
