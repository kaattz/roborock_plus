"""Prove the pre-fix dock sensor fails at runtime under python-roborock 7.x.

``RoborockDockTypeCode.no_dock`` is referenced inside ``_dock_error_value_fn``,
so merely importing the module still succeeds. The failure only appears when the
sensor reads its value -- i.e. on every coordinator update, long after setup.
This script builds the two variants and calls the function.

    .analysis/venvha314/Scripts/python.exe scripts/prove_dock_member_failure.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENSOR = ROOT / "custom_components" / "roborock_plus" / "sensor.py"

PROBE = textwrap.dedent(
    """
    import sys
    from types import SimpleNamespace
    sys.path.insert(0, {path!r})
    from roborock_plus import sensor
    from roborock.data import RoborockDockErrorCode, RoborockDockTypeCode

    docked = SimpleNamespace(status=SimpleNamespace(
        dock_error_status=RoborockDockErrorCode(0),
        dock_type=RoborockDockTypeCode.o0_dock))
    undocked = SimpleNamespace(status=SimpleNamespace(
        dock_error_status=RoborockDockErrorCode(0),
        dock_type=RoborockDockTypeCode.o1_dock))

    print("no-dock ->", sensor._dock_error_value_fn(docked))
    print("with-dock ->", sensor._dock_error_value_fn(undocked))
    """
)


def run(component_dir: Path) -> tuple[bool, str]:
    script = PROBE.format(path=str(component_dir))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,  # the return code is the observation, not an error
    )
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output


def main() -> int:
    from importlib.metadata import version

    import roborock  # noqa: F401

    print(f"python-roborock: {version('python-roborock')}\n")

    original = SENSOR.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "custom_components"
        root.mkdir(parents=True)
        component = root / "roborock_plus"
        component.mkdir()

        # Copy the component, then write the pre-fix variant of sensor.py.
        for path in (ROOT / "custom_components" / "roborock_plus").glob("*.py"):
            (component / path.name).write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )

        (component / "sensor.py").write_text(
            original.replace("RoborockDockTypeCode.o0_dock", "RoborockDockTypeCode.no_dock"),
            encoding="utf-8",
        )
        broke_ok, broke_out = run(root)
        print("=== PRE-FIX (RoborockDockTypeCode.no_dock) ===")
        print(f"  value function ran: {broke_ok}")
        for line in broke_out.splitlines()[-6:]:
            print(f"    {line}")

        (component / "sensor.py").write_text(original, encoding="utf-8")
        fixed_ok, fixed_out = run(root)
        print("\n=== FIXED (RoborockDockTypeCode.o0_dock) ===")
        print(f"  value function ran: {fixed_ok}")
        for line in fixed_out.splitlines()[-4:]:
            print(f"    {line}")

    print()
    problems: list[str] = []
    if broke_ok:
        problems.append("pre-fix code did NOT fail; the regression would be invisible")
    if not fixed_ok:
        problems.append("fixed code failed; the fix is wrong")
    if "no attribute 'no_dock'" not in broke_out:
        problems.append("pre-fix failure was not the expected AttributeError")

    if problems:
        for problem in problems:
            print(f"FAIL: {problem}")
        return 1
    print("PASS: pre-fix raises AttributeError at runtime; fixed code returns values")
    return 0


if __name__ == "__main__":
    sys.exit(main())
