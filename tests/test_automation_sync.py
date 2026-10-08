"""Tests for the automation sync scripts.

The two scripts touch `automations.yaml`, which holds 109 automations belonging
to the wider installation while only three belong to this repo. A bug here can
corrupt unrelated automations, so the guards are load-bearing and are tested as
such rather than assumed.

These tests exercise the pure logic -- the id matching, the payload shape, the
refusal conditions -- without needing a live Home Assistant.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
AUTOMATIONS_DIR = ROOT / "automations"


def _load(name: str):
    """Import a script module without running its main()."""
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"sync_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FETCH = _load("fetch_automations")
SYNC = _load("sync_automations")


class TestWatchedSetsAgree:
    """Both scripts must agree on what they manage, or they will fight."""

    def test_both_scripts_watch_the_same_ids(self) -> None:
        assert FETCH.WATCHED == SYNC.WATCHED

    def test_every_watched_id_has_a_file(self) -> None:
        for filename in SYNC.WATCHED.values():
            assert (AUTOMATIONS_DIR / filename).exists(), f"missing {filename}"


class TestRepoFilesMatchTheirIds:
    """The id in the file is the handle; a mismatch would replace the wrong one."""

    @pytest.mark.parametrize("unique_id,filename", sorted(SYNC.WATCHED.items()))
    def test_file_id_matches_its_key(self, unique_id: str, filename: str) -> None:
        config = yaml.safe_load((AUTOMATIONS_DIR / filename).read_text(encoding="utf-8"))
        assert str(config["id"]) == unique_id

    @pytest.mark.parametrize("filename", sorted(SYNC.WATCHED.values()))
    def test_aliases_are_english(self, filename: str) -> None:
        """The whole point of the rename: no pinyin aliases."""
        config = yaml.safe_load((AUTOMATIONS_DIR / filename).read_text(encoding="utf-8"))
        alias = config["alias"]
        assert alias.isascii(), f"{filename}: alias is not ASCII: {alias!r}"
        assert alias.startswith("Roborock "), (
            f"{filename}: alias should carry the integration prefix: {alias!r}"
        )

    @pytest.mark.parametrize("filename", sorted(SYNC.WATCHED.values()))
    def test_safe_to_embed_in_a_larger_file(self, filename: str) -> None:
        """The repo file is a bare mapping, not a list item.

        It gets spliced into the list in automations.yaml, so a document that
        parsed as a list would replace the whole file instead of one entry.
        """
        config = yaml.safe_load((AUTOMATIONS_DIR / filename).read_text(encoding="utf-8"))
        assert isinstance(config, dict)


class TestWorkerGuards:
    """The guards inside the container worker are the real safety net."""

    def _worker(self) -> str:
        return SYNC.WORKER

    def test_round_trip_is_verified_before_writing(self) -> None:
        worker = self._worker()
        assert "round_tripped != automations" in worker
        assert "REFUSING" in worker
        # The check must come before the write, not after.
        assert worker.index("round_tripped != automations") < worker.index(
            "shutil.copy2(path, backup)"
        )

    def test_duplicate_ids_are_refused(self) -> None:
        """Two entries with the same id would make the target ambiguous."""
        worker = self._worker()
        assert "duplicate id" in worker

    def test_missing_ids_are_refused(self) -> None:
        worker = self._worker()
        assert "not found in automations.yaml" in worker

    def test_a_backup_is_taken_before_writing(self) -> None:
        worker = self._worker()
        assert worker.index("shutil.copy2(path, backup)") < worker.index(
            'with open(path, "w"'
        )

    def test_the_write_is_verified_and_rolled_back_on_mismatch(self) -> None:
        """A failed verification must restore, not leave a half-written file."""
        worker = self._worker()
        assert "WRITE VERIFICATION FAILED" in worker
        assert "shutil.copy2(backup, path)" in worker

    def test_count_is_checked_after_writing(self) -> None:
        """A dropped entry is the failure mode a field compare would miss."""
        worker = self._worker()
        assert "count changed" in worker

    def test_only_watched_entries_are_replaced(self) -> None:
        """Unrelated automations must be re-dumped, never rebuilt."""
        worker = self._worker()
        assert "for key, config in replacements.items()" in worker
        assert "automations[index] = config" in worker


class TestPayloadIsPassedAsAFile:
    """A ~15 KB payload over argv was silently truncated once already."""

    def test_worker_reads_the_payload_from_a_path(self) -> None:
        assert 'open(sys.argv[2], encoding="utf-8")' in SYNC.WORKER

    def test_runner_writes_the_payload_to_a_file(self) -> None:
        source = (SCRIPTS / "sync_automations.py").read_text(encoding="utf-8")
        assert "_sync.json" in source
        assert "base64 -d" in source


class TestSyncAndFetchAreSeparate:
    """One script doing both directions could accept its own output."""

    def test_fetch_never_writes_to_the_container(self) -> None:
        source = (SCRIPTS / "fetch_automations.py").read_text(encoding="utf-8")
        reader = source.split("READER = r'''")[1].split("'''")[0]
        assert "safe_dump" not in reader
        assert 'open("/config/automations.yaml", "w"' not in reader

    def test_fetch_requires_explicit_write_for_the_repo(self) -> None:
        source = (SCRIPTS / "fetch_automations.py").read_text(encoding="utf-8")
        assert '"--write" in sys.argv' in source

    def test_sync_requires_explicit_apply_for_the_container(self) -> None:
        source = (SCRIPTS / "sync_automations.py").read_text(encoding="utf-8")
        assert '"--apply" in sys.argv' in source


class TestNoSecretsInTrackedFiles:
    """The repo is public. The SSH password must never be committed again.

    It already was: an earlier revision hardcoded it in three scripts that are
    still in the pushed history. These tests stop it coming back through the
    files added since, and the broader scan in test_no_committed_secrets.py
    covers the rest of the repository.
    """

    @pytest.mark.parametrize(
        "name", ["sync_automations.py", "fetch_automations.py"]
    )
    def test_password_is_read_from_the_environment(self, name: str) -> None:
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert 'os.environ.get("ROBOROCK_HA_PASSWORD")' in source

    @pytest.mark.parametrize(
        "name", ["sync_automations.py", "fetch_automations.py"]
    )
    def test_no_literal_password(self, name: str) -> None:
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert 'PASSWORD = "' not in source, (
            f"{name}: password looks hardcoded; use the environment"
        )

    @pytest.mark.parametrize(
        "name", ["sync_automations.py", "fetch_automations.py"]
    )
    def test_missing_password_is_an_explicit_error(self, name: str) -> None:
        """Fail loudly with instructions rather than a confusing auth error."""
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "ROBOROCK_HA_PASSWORD is not set" in source
