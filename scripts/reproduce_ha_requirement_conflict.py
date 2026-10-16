"""Reproduce HA's requirement-resolution failure and prove the new pin fixes it.

Home Assistant 2026.10.1 ships ``protobuf==7.36.0`` in package_constraints.txt
and installs each custom integration's manifest requirements on top of that
frozen set. This script asks a real resolver for the same thing HA asks for.

Run::

    python scripts/reproduce_ha_requirement_conflict.py

Exit 0 means: the broken pin fails to resolve and the fixed pin resolves, in a
throwaway environment, with no network side effects beyond package metadata.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# What HA 2026.10.1 freezes (homeassistant/package_constraints.txt).
HA_PROTOBUF = "protobuf==7.36.0"

BROKEN = ["python-roborock==5.5.1", "vacuum-map-parser-roborock==0.1.4"]
FIXED = ["python-roborock==7.12.0", "vacuum-map-parser-roborock==0.1.5"]


def resolve(requirements: list[str], workdir: Path) -> tuple[bool, str]:
    """Resolve the requirement set without installing; return (ok, output).

    ``uv pip compile`` runs the resolver alone, so it needs no interpreter or
    virtual environment and cannot touch the developer's site-packages. The
    frozen HA constraint is always part of the input, because that is what
    makes the resolution fail on the old pin.
    """
    uv = shutil.which("uv")
    if uv is None:
        return False, "uv not found on PATH; install uv to run this check"

    cmd = [
        uv,
        "pip",
        "compile",
        "-",
        "--quiet",
        "--no-cache",
        "--python-version",
        "3.13",
    ]
    payload = "\n".join([*requirements, HA_PROTOBUF]) + "\n"
    proc = subprocess.run(  # noqa: PLW1510
        cmd,
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=workdir,
        timeout=600,
    )
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output


def resolved_protobuf(output: str) -> str:
    """Pull the resolved protobuf line out of the compile output."""
    for line in output.splitlines():
        if line.lower().startswith("protobuf=="):
            return line.strip()
    return "<not in output>"


def headline(output: str, limit: int = 14) -> str:
    """Show the resolver's verdict, skipping noisy wheel-skipping warnings."""
    lines = [
        line
        for line in output.splitlines()
        if line.strip() and not line.strip().startswith("WARN")
    ]
    safe = [
        line.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace"
        )
        for line in lines[:limit]
    ]
    return "\n".join(f"      {line}" for line in safe)


def main() -> int:
    manifest = (ROOT / "custom_components" / "roborock_plus" / "manifest.json").read_text(
        encoding="utf-8"
    )

    print(f"HA frozen constraint: {HA_PROTOBUF}\n")

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)

        print("=== OLD pin (what v0.7.6 shipped) ===")
        print(f"  requirements: {BROKEN}")
        broken_ok, broken_out = resolve(BROKEN, workdir)
        print(f"  resolvable: {broken_ok}")
        if broken_ok:
            print(f"  resolved protobuf: {resolved_protobuf(broken_out)}")
        else:
            print(headline(broken_out, limit=10))
        if broken_ok:
            print("  UNEXPECTED: the broken pin resolved; the repro is invalid")

        print("\n=== NEW pin (this fix) ===")
        print(f"  requirements: {FIXED}")
        fixed_ok, fixed_out = resolve(FIXED, workdir)
        print(f"  resolvable: {fixed_ok}")
        if fixed_ok:
            print(f"  resolved protobuf: {resolved_protobuf(fixed_out)}")
        else:
            print(headline(fixed_out, limit=10))

    print("\n=== manifest on disk ===")
    in_manifest_fixed = "python-roborock==7.12.0" in manifest
    in_manifest_broken = "python-roborock==5.5.1" in manifest
    print(f"  contains python-roborock==7.12.0: {in_manifest_fixed}")
    print(f"  contains python-roborock==5.5.1: {in_manifest_broken}")

    problems: list[str] = []
    if broken_ok:
        problems.append("old pin unexpectedly resolvable (repro would be meaningless)")
    if not fixed_ok:
        problems.append("new pin does NOT resolve -- fix is wrong")
    if not in_manifest_fixed or in_manifest_broken:
        problems.append("manifest on disk does not carry the fixed pin")

    print()
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}")
        return 1
    print("PASS: old pin is unsatisfiable, new pin resolves, manifest carries the fix")
    return 0


if __name__ == "__main__":
    sys.exit(main())
