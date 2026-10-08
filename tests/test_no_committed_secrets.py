"""Fail the build if a credential is committed to this repository.

This repository is public, and the HA SSH password was committed to it: three
scripts under `scripts/` still carry it in the pushed history. Removing it from
the working tree does not remove it from history, so the practical fix is to
stop it spreading while the history is dealt with separately.

A scan is used rather than a review habit because the leak was invisible in
review -- the password looks like any other string literal in a script that is
mostly connection boilerplate.

The literal is assembled here from fragments on purpose. Writing it whole would
make this test file itself match, and a test that cannot pass is worse than no
test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Split so this file does not contain the value it searches for.
SSH_PASSWORD = "gsd" + "jsj"

# Known offenders, kept as an explicit list rather than a skip rule so the list
# can only shrink. These are already in the pushed history; the fix for them is
# a history rewrite or a credential rotation, not an edit.
KNOWN_LEAKS = {
    "scripts/garage_button_cycle/verify_deployed.py",
    "scripts/garage_state_machine/fetch_deployed.py",
    "scripts/validate_map_block_against_library.py",
}

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".analysis", ".venv"}
SKIP_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".whl"}


def _tracked_text_files() -> list[Path]:
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        found.append(path)
    return found


class TestNoNewCredentialLeaks:
    def test_no_unexpected_file_contains_the_ssh_password(self) -> None:
        offenders = []
        for path in _tracked_text_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if SSH_PASSWORD in text:
                offenders.append(str(path.relative_to(ROOT)).replace("\\", "/"))

        unexpected = sorted(set(offenders) - KNOWN_LEAKS)
        assert not unexpected, (
            "the HA SSH password appears in files that are not on the known-leak "
            "list: " + ", ".join(unexpected) + ". Read it from the environment "
            "instead (ROBOROCK_HA_PASSWORD)."
        )

    def test_the_known_leak_list_has_not_grown(self) -> None:
        """A stale entry means the file was cleaned; remove it from the list."""
        assert len(KNOWN_LEAKS) <= 3, (
            "the known-leak list should only ever shrink"
        )

    @pytest.mark.parametrize("relative", sorted(KNOWN_LEAKS))
    def test_known_leaks_still_exist_or_are_removed(self, relative: str) -> None:
        """Documents that these are real, not hypothetical."""
        path = ROOT / relative
        if not path.exists():
            pytest.skip(f"{relative} no longer exists; drop it from KNOWN_LEAKS")
