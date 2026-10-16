"""Mutation check: the migration guard tests must go red when the bug returns.

A guard test that passes on both the broken and the fixed tree proves nothing.
This reverts each defect in a temporary copy of the tree and asserts the
matching test fails. Restores every file afterwards.

    python scripts/mutation_check_ha_2026_10_1_requirements.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "roborock_plus"
TESTS = ROOT / "tests"
TEST_FILE = "test_manifest_dependency_compatibility.py"


class Mutation:
    def __init__(self, name: str, relative: Path, old: str, new: str, expected_test: str):
        self.name = name
        self.relative = relative
        self.old = old
        self.new = new
        self.expected_test = expected_test


MUTATIONS = [
    Mutation(
        name="revert python-roborock pin to the broken 5.5.1",
        relative=Path("manifest.json"),
        old='"python-roborock==7.12.0"',
        new='"python-roborock==5.5.1"',
        expected_test="test_python_roborock_major_allows_protobuf_7",
    ),
    Mutation(
        name="reintroduce the removed RoborockDockTypeCode.no_dock member",
        relative=Path("sensor.py"),
        old="RoborockDockTypeCode.o0_dock",
        new="RoborockDockTypeCode.no_dock",
        expected_test="test_sensor_module_does_not_use_removed_no_dock_member",
    ),
    Mutation(
        name="restore the stale vacuum-map-parser-roborock pin",
        relative=Path("manifest.json"),
        old='"vacuum-map-parser-roborock==0.1.5"',
        new='"vacuum-map-parser-roborock==0.1.4"',
        expected_test="test_map_parser_matches_the_version_used_by_the_audited_pin",
    ),
]


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(
            ".git", ".analysis", "__pycache__", ".pytest_cache", ".ruff_cache"
        ))

        target_test = work / "tests" / TEST_FILE
        if not target_test.exists():
            print(f"FATAL: {target_test} missing from the copy")
            return 2

        for mutation in MUTATIONS:
            path = work / "custom_components" / "roborock_plus" / mutation.relative
            original = path.read_text(encoding="utf-8")
            if mutation.old not in original:
                failures.append(f"{mutation.name}: anchor not found in {mutation.relative}")
                print(f"SKIP  {mutation.name}: anchor {mutation.old!r} not present")
                continue

            path.write_text(original.replace(mutation.old, mutation.new, 1), encoding="utf-8")
            try:
                proc = subprocess.run(  # noqa: PLW1510
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        str(target_test),
                        "-q",
                        "--no-header",
                        "-p",
                        "no:cacheprovider",
                        "-k",
                        mutation.expected_test,
                    ],
                    cwd=work,
                    capture_output=True,
                    text=True,
                )
            finally:
                path.write_text(original, encoding="utf-8")

            if proc.returncode == 0:
                failures.append(mutation.name)
                print(f"BAD   {mutation.name}")
                print("      guard stayed GREEN -> the test does not catch this defect")
            else:
                tail = [line for line in proc.stdout.strip().splitlines() if line.strip()][-1:]
                print(f"OK    {mutation.name}")
                print(f"      guard went RED as required ({tail[0] if tail else 'failed'})")

    print()
    if failures:
        print(f"mutation check FAILED for {len(failures)} mutation(s):")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"all {len(MUTATIONS)} mutations were caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
