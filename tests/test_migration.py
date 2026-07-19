import json
import os
import shutil

import pytest

from pony.state.file_lock import locked_file
from pony.state.migration import ABSENT, ROLLED_BACK, Migration, _MAX_JOURNAL_BYTES
from pony.security.private_files import PrivateAtomicWriteError


def migration(tmp_path, validate=lambda path: True):
    root = tmp_path / ".pony"
    root.mkdir(mode=0o700)
    live = root / "runs"
    live.mkdir()
    (live / "value").write_text("old")
    return Migration(
        root,
        contract="run_artifacts",
        source_version=1,
        target_version=2,
        live="runs",
        workspace_identity={"repo_commit": "abc", "repo_dirty": False},
        validate=validate,
    )


def builder(source, candidate):
    shutil.copytree(source, candidate)
    (candidate / "value").write_text("new")


def test_apply_commits_and_uses_owner_only_area(tmp_path):
    item = migration(tmp_path)
    assert item.apply(builder) == ABSENT
    assert (item.live / "value").read_text() == "new"
    assert item.area.stat().st_mode & 0o777 == 0o700


def test_manifest_hashes_files_without_unbounded_path_read(tmp_path, monkeypatch):
    item = migration(tmp_path)

    monkeypatch.setattr(
        type(item.live / "value"),
        "read_bytes",
        lambda _path: pytest.fail("manifest used unbounded Path.read_bytes"),
    )

    assert item.apply(builder) == ABSENT


def test_apply_fsyncs_removed_rollback_and_journal_parents(tmp_path, monkeypatch):
    item = migration(tmp_path)
    fsyncs = []
    monkeypatch.setattr(item, "_fsync_dir", lambda path: fsyncs.append(path))

    assert item.apply(builder) == ABSENT

    assert item.rollback.parent in fsyncs
    assert item.journal.parent in fsyncs


@pytest.mark.parametrize("failure", ("rollback_parent", "journal_parent"))
def test_recover_after_cleanup_fsync_failure(tmp_path, monkeypatch, failure):
    item = migration(tmp_path)
    fsync_dir = item._fsync_dir
    failed = False

    def fail_once(path):
        nonlocal failed
        rollback_removed = path == item.rollback.parent and not item.rollback.exists()
        journal_removed = path == item.journal.parent and not item.journal.exists()
        should_fail = (
            failure == "rollback_parent"
            and rollback_removed
            or failure == "journal_parent"
            and journal_removed
        )
        if should_fail and not failed:
            failed = True
            raise OSError("injected cleanup fsync failure")
        fsync_dir(path)

    monkeypatch.setattr(item, "_fsync_dir", fail_once)
    with pytest.raises(OSError, match="cleanup fsync failure"):
        item.apply(builder)

    monkeypatch.setattr(item, "_fsync_dir", fsync_dir)
    assert item.recover() == ABSENT
    assert (item.live / "value").read_text() == "new"


def test_reader_failure_rolls_back(tmp_path):
    item = migration(tmp_path, validate=lambda path: False)
    assert item.apply(builder) == ROLLED_BACK
    assert (item.live / "value").read_text() == "old"


def test_committed_rolled_back_journal_is_not_rewritten_as_failed(
    tmp_path,
    monkeypatch,
):
    item = migration(tmp_path, validate=lambda path: False)
    original_write = item._write

    def commit_then_raise(value, state, error=""):
        result = original_write(value, state, error)
        if state == ROLLED_BACK:
            raise PrivateAtomicWriteError("ambiguous committed journal")
        return result

    monkeypatch.setattr(item, "_write", commit_then_raise)

    with pytest.raises(PrivateAtomicWriteError, match="ambiguous committed journal"):
        item.apply(builder)

    assert item._read()["state"] == ROLLED_BACK
    assert (item.live / "value").read_text() == "old"


def test_recovers_crash_after_each_rename(tmp_path, monkeypatch):
    item = migration(tmp_path)
    original = item._write

    def crash(value, state, error=""):
        result = original(value, state, error)
        if state == "old_moved":
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(item, "_write", crash)
    with pytest.raises(KeyboardInterrupt):
        item.apply(builder)
    monkeypatch.setattr(item, "_write", original)
    assert item.recover() == ABSENT
    assert (item.live / "value").read_text() == "new"


def test_recovers_crash_between_rename_and_journal_write(tmp_path, monkeypatch):
    item = migration(tmp_path)
    original = item._write

    def crash(value, state, error=""):
        if state == "old_moved":
            raise KeyboardInterrupt
        return original(value, state, error)

    monkeypatch.setattr(item, "_write", crash)
    with pytest.raises(KeyboardInterrupt):
        item.apply(builder)
    monkeypatch.setattr(item, "_write", original)
    assert item.recover() == ABSENT
    assert (item.live / "value").read_text() == "new"


def test_recover_cleans_completed_rollback(tmp_path):
    item = migration(tmp_path, validate=lambda path: False)
    assert item.apply(builder) == ROLLED_BACK
    assert item.recover() == ABSENT
    assert item.status()["state"] == ABSENT


def test_abort_fsyncs_removed_candidate_and_journal_parents(tmp_path, monkeypatch):
    item = migration(tmp_path)
    _leave_candidate_ready(item, monkeypatch)
    fsyncs = []
    monkeypatch.setattr(item, "_fsync_dir", lambda path: fsyncs.append(path))

    assert item.abort() == ABSENT

    assert item.candidate.parent in fsyncs
    assert item.journal.parent in fsyncs


def test_recover_finishes_abort_after_candidate_cleanup_fsync_failure(
    tmp_path, monkeypatch
):
    item = migration(tmp_path)
    _leave_candidate_ready(item, monkeypatch)
    fsync_dir = item._fsync_dir
    failed = False

    def fail_once(path):
        nonlocal failed
        if path == item.candidate.parent and not item.candidate.exists() and not failed:
            failed = True
            raise OSError("injected candidate cleanup fsync failure")
        fsync_dir(path)

    monkeypatch.setattr(item, "_fsync_dir", fail_once)
    with pytest.raises(OSError, match="candidate cleanup fsync failure"):
        item.abort()

    monkeypatch.setattr(item, "_fsync_dir", fsync_dir)
    assert item.recover() == ABSENT
    assert (item.live / "value").read_text() == "old"


def test_lock_reentry_is_rejected(tmp_path):
    item = migration(tmp_path)
    item._setup()
    with locked_file(item.lock, require_lock=True):
        with pytest.raises(RuntimeError, match="reentry"):
            item.apply(builder)


def test_symlink_in_candidate_is_rejected(tmp_path):
    item = migration(tmp_path)

    def unsafe(source, candidate):
        candidate.mkdir()
        (candidate / "link").symlink_to(source / "value")

    with pytest.raises(ValueError, match="unsafe"):
        item.apply(unsafe)


def test_identity_change_is_rejected(tmp_path):
    item = migration(tmp_path)
    original = item._advance

    def stop(value):
        value["workspace_identity"] = {"repo_commit": "other", "repo_dirty": False}
        item._write(value, "candidate_ready")
        raise KeyboardInterrupt

    item._advance = stop
    with pytest.raises(KeyboardInterrupt):
        item.apply(builder)
    item._advance = original
    with pytest.raises(ValueError, match="identity_mismatch"):
        item.recover()


def test_exact_journal_rejects_unknown_and_duplicate_keys(tmp_path):
    item = migration(tmp_path)
    item._setup()
    item.journal.write_text('{"state":"preparing","state":"preparing"}')
    with pytest.raises(ValueError, match="duplicate"):
        item.recover()
    item.journal.write_text(json.dumps({"unknown": 1}))
    with pytest.raises(ValueError, match="schema"):
        item.recover()


def test_journal_write_is_bounded(tmp_path):
    item = migration(tmp_path)
    item._setup()
    value = {
        "record_type": "migration_journal",
        "format_version": 1,
        "migration_id": "mig_test",
        "contract": item.contract,
        "source_version": item.source_version,
        "target_version": item.target_version,
        "state": "preparing",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "workspace_identity": item.workspace_identity,
        "paths": {
            "live": item.live_rel.as_posix(),
            "candidate": item.candidate_rel.as_posix(),
            "rollback": item.rollback_rel.as_posix(),
        },
        "source_identity": {},
        "candidate_identity": {},
        "error_code": "",
    }

    with pytest.raises(ValueError, match="private file too large"):
        item._write(value, value["state"], "x" * _MAX_JOURNAL_BYTES)


def test_journal_rejects_oversized_existing_artifact_before_backup(tmp_path):
    item = migration(tmp_path)
    item._setup()
    value = {
        "record_type": "migration_journal",
        "format_version": 1,
        "migration_id": "mig_test",
        "contract": item.contract,
        "source_version": item.source_version,
        "target_version": item.target_version,
        "state": "preparing",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "workspace_identity": item.workspace_identity,
        "paths": {
            "live": item.live_rel.as_posix(),
            "candidate": item.candidate_rel.as_posix(),
            "rollback": item.rollback_rel.as_posix(),
        },
        "source_identity": {},
        "candidate_identity": {},
        "error_code": "",
    }
    oversized = b"x" * (_MAX_JOURNAL_BYTES + 1)
    item.journal.write_bytes(oversized)

    with pytest.raises(ValueError, match="private file too large"):
        item._write(value, value["state"])

    assert item.journal.read_bytes() == oversized
    assert not list(item.area.glob(".*.bak"))


def _leave_candidate_ready(item, monkeypatch):
    original = item._advance

    def stop(_value):
        raise KeyboardInterrupt

    monkeypatch.setattr(item, "_advance", stop)
    with pytest.raises(KeyboardInterrupt):
        item.apply(builder)
    monkeypatch.setattr(item, "_advance", original)


def test_replaced_migration_area_is_rejected_before_rename(tmp_path, monkeypatch):
    item = migration(tmp_path)
    _leave_candidate_ready(item, monkeypatch)
    original_area = tmp_path / "original-migration-area"
    item.area.rename(original_area)
    shutil.copytree(original_area, item.area)

    monkeypatch.setattr(
        item, "_rename", lambda *_: pytest.fail("replaced area reached rename")
    )
    with pytest.raises(ValueError, match="area changed"):
        item.apply()


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("symlink", "symlink"),
        ("hardlink", "multiple links"),
        ("fifo", "regular"),
        ("oversize", "too large"),
    ],
)
def test_untrusted_journal_is_rejected_before_rename(
    tmp_path, monkeypatch, kind, message
):
    item = migration(tmp_path)
    _leave_candidate_ready(item, monkeypatch)
    outside = tmp_path / "outside-journal.json"

    if kind == "symlink":
        outside.write_bytes(item.journal.read_bytes())
        item.journal.unlink()
        item.journal.symlink_to(outside)
    elif kind == "hardlink":
        outside.write_bytes(item.journal.read_bytes())
        item.journal.unlink()
        os.link(outside, item.journal)
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unsupported")
        item.journal.unlink()
        os.mkfifo(item.journal, 0o600)
    else:
        item.journal.write_bytes(b"x" * (_MAX_JOURNAL_BYTES + 1))

    monkeypatch.setattr(
        item, "_rename", lambda *_: pytest.fail("untrusted journal reached rename")
    )
    with pytest.raises(ValueError, match=message):
        item.apply()

    assert (item.live / "value").read_text() == "old"
    assert (item.candidate / "value").read_text() == "new"
