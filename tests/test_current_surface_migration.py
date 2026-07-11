import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from pico import current_surface_migration as migration


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(data)
    path.chmod(0o600)


def _session(index, embedded):
    return {
        "id": f"s{index}",
        "schema_version": 2 if index % 2 == 0 else 3,
        "created_at": f"2026-07-11T00:00:0{index}Z",
        "workspace_root": "/workspace",
        "messages": [
            {"role": "user", "content": f"message-{index}", "_pico_meta": {}}
        ],
        "history": [] if index % 2 == 0 else None,
        "working_memory": {},
        "memory": {},
        "recently_recalled": [],
        "checkpoints": {
            "items": {
                f"embedded-{index}-{item}": {
                    "schema_version": "phase1-v1",
                    "runtime_identity": {
                        "feature_flags": {"prompt_cache": True, "real": True}
                    },
                }
                for item in range(embedded)
            }
        },
        "resume_state": {},
        "recovery": {},
        "runtime_identity": {
            "feature_flags": {"prompt_cache": True, "real": True}
        },
    }


def _checkpoint(index):
    return {
        "schema_version": "checkpoint-record-v1",
        "checkpoint_id": f"ck{index}",
        "checkpoint_type": "turn",
        "created_at": f"2026-07-11T01:00:0{index}Z",
        "workspace_root": "/workspace",
        "tool_change_ids": [f"tc{index}"],
        "verification_evidence": [
            {
                "schema_version": "verification-record-v1",
                "verification_id": f"v{index}",
                "created_at": "2026-07-11T03:00:00Z",
                "argv": ["pytest"],
                "runner_executed": True,
                "execution_mode": "argv",
                "command": "pytest",
                "risk_class": "safe",
                "exit_code": 0,
                "status": "passed",
                "stdout_tail": "",
                "stderr_tail": "",
                "affected_checkpoint_id": f"ck{index}",
                "trace_event_id": f"trace{index}",
            }
        ],
    }


def _tool_change(index):
    return {
        "schema_version": "tool-change-record-v1",
        "tool_change_id": f"tc{index}",
        "checkpoint_id": f"ck{index}",
        "started_at": f"2026-07-11T02:00:0{index}Z",
        "tool_name": "write_file",
        "effect_class": "workspace_write",
        "status": "finalized",
    }


@pytest.fixture
def surface(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    pico = repo / ".pico"
    records = {
        **{
            f"sessions/s{index}.json": _session(index, 8 if index < 2 else 7)
            for index in range(4)
        },
        **{
            f"checkpoints/records/ck{index}.json": _checkpoint(index)
            for index in range(2)
        },
        **{
            f"checkpoints/tool_changes/tc{index}.json": _tool_change(index)
            for index in range(2)
        },
    }
    for relative, value in records.items():
        if relative.startswith("sessions/s") and value.get("schema_version") == 3:
            value.pop("history")
        _write(pico / relative, (json.dumps(value, indent=2) + "\n").encode())
    _write(pico / "sessions/.session_store.lock", b"")
    _write(pico / "checkpoints/.checkpoint_store.lock", b"")
    for index in range(36):
        _write(pico / f"runs/run-{index:02d}.json", f"run-{index}\n".encode())

    transform_paths = set(records)
    entries = []
    for path in sorted(item for item in pico.rglob("*") if item.is_file()):
        raw = path.read_bytes()
        info = path.stat()
        relative = path.relative_to(pico).as_posix()
        entries.append(
            {
                "path": relative,
                "role": "transform" if relative in transform_paths else "verify_only",
                "device": info.st_dev,
                "inode": info.st_ino,
                "nlink": info.st_nlink,
                "mode": info.st_mode & 0o777,
                "mtime_ns": info.st_mtime_ns,
                "size": info.st_size,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    repo_hash = hashlib.sha256(str(repo).encode()).hexdigest()[:16]
    manifest = {
        "record_type": "current_surface_preflight",
        "created_at": "2026-07-11T00:00:00Z",
        "git_head": "1" * 40,
        "repo_hash": repo_hash,
        "checkpoint_lock_mode_before": "0600",
        "summary": migration._SUMMARY,
        "entries": entries,
    }
    manifest_path = home / ".pico/backups" / repo_hash / "preflight/manifest.json"
    _write(manifest_path, (json.dumps(manifest) + "\n").encode())
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PICO_PLAN1_MANIFEST", str(manifest_path))
    monkeypatch.setattr(migration, "_migration_commit", lambda _root: "2" * 40)
    return repo, home, manifest


def _transaction(home, manifest):
    root = home / ".pico/backups" / manifest["repo_hash"]
    transactions = list(root.glob("migration-*"))
    assert len(transactions) == 1
    return transactions[0], json.loads((transactions[0] / "journal.json").read_text())


def _write_journal_direct(root, journal):
    path = root / "journal.json"
    path.write_text(json.dumps(journal) + "\n")
    path.chmod(0o600)


def _refresh_manifest_entry(manifest, relative):
    path = Path.cwd() / ".pico" / relative
    raw = path.read_bytes()
    info = path.stat()
    item = next(item for item in manifest["entries"] if item["path"] == relative)
    item.update(
        device=info.st_dev,
        inode=info.st_ino,
        nlink=info.st_nlink,
        mode=info.st_mode & 0o777,
        mtime_ns=info.st_mtime_ns,
        size=info.st_size,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    manifest_path = Path(os.environ["PICO_PLAN1_MANIFEST"])
    manifest_path.write_text(json.dumps(manifest) + "\n")
    manifest_path.chmod(0o600)


def test_full_surface_applies_verifies_and_is_idempotent(surface, capsys):
    repo, home, manifest = surface
    verify_only = {
        item["path"]: (repo / ".pico" / item["path"]).read_bytes()
        for item in manifest["entries"] if item["role"] == "verify_only"
    }

    migration.apply()
    migration.apply()
    migration.verify()

    _, journal = _transaction(home, manifest)
    assert journal["status"] == "verified"
    assert len(journal["applied_paths"]) == 8
    assert all((repo / ".pico" / path).read_bytes() == raw for path, raw in verify_only.items())
    assert "transformed=8 verify_only=38" in capsys.readouterr().out


@pytest.mark.parametrize(
    "fault",
    [
        "staging:parent-fsync",
        "backup:directory-fsync",
        *(f"backup:{index}:file-fsync" for index in range(8)),
        *(f"backup:{index}:parent-fsync" for index in range(8)),
        *(f"backup:{index}" for index in range(8)),
        "backup:root-fsync",
        "journal:prepared:file-fsync",
        "journal:prepared:parent-fsync",
        "prepared",
        "promotion:parent-fsync",
        "promoted",
        "journal:applying:file-fsync",
        "journal:applying:parent-fsync",
        *(
            f"replace:{index}:{point}"
            for index in range(8)
            for point in (
                "temp",
                "before-swap",
                "after-swap",
                "before-metadata",
                "after-metadata",
                "after-cleanup",
            )
        ),
        *(f"replace:{index}" for index in range(8)),
        "journal:verified:file-fsync",
        "journal:verified:parent-fsync",
    ],
)
def test_crash_resumes_same_transaction(surface, monkeypatch, fault):
    _, home, manifest = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", fault)
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")

    migration.apply()

    _, journal = _transaction(home, manifest)
    assert journal["status"] == "verified"


def test_applying_transaction_can_rollback_and_verify_original(surface, monkeypatch):
    repo, home, manifest = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", "replace:2")
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")

    migration.rollback()
    migration.verify_original()

    _, journal = _transaction(home, manifest)
    assert journal["status"] == "applying"
    for item in manifest["entries"]:
        assert hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest() == item["sha256"]

    migration.apply()
    migration.verify()
    _, journal = _transaction(home, manifest)
    assert journal["status"] == "verified"


def test_manifest_rejects_extra_file_without_backup(surface):
    repo, home, manifest = surface
    _write(repo / ".pico/unlisted", b"no")

    with pytest.raises(migration.MigrationError, match="path set"):
        migration.apply()

    backup_root = home / ".pico/backups" / manifest["repo_hash"]
    assert not list(backup_root.glob("migration-*"))


def test_duplicate_nested_json_is_rejected_content_free(surface):
    repo, _, manifest = surface
    target = repo / ".pico/sessions/s0.json"
    target.write_bytes(b'{"schema_version":2,"id":"s0","id":"secret-canary"}')
    target.chmod(0o600)
    item = next(item for item in manifest["entries"] if item["path"] == "sessions/s0.json")
    raw = target.read_bytes()
    info = target.stat()
    item.update(inode=info.st_ino, mtime_ns=info.st_mtime_ns, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    manifest_path = Path(os.environ["PICO_PLAN1_MANIFEST"])
    manifest_path.write_text(json.dumps(manifest) + "\n")
    manifest_path.chmod(0o600)

    with pytest.raises(migration.MigrationError) as caught:
        migration.apply()
    assert "secret-canary" not in str(caught.value)


def test_verified_transaction_refuses_rollback(surface):
    migration.apply()
    with pytest.raises(migration.MigrationError, match="cannot be rolled back"):
        migration.rollback()


def test_ordinary_verification_failure_rolls_back_every_target(surface, monkeypatch):
    repo, _, manifest = surface

    def fail(*_args):
        raise migration.MigrationError("strict migration verification failed")

    monkeypatch.setattr(migration, "_strict_verify_locked", fail)
    with pytest.raises(migration.MigrationError):
        migration.apply()

    for item in manifest["entries"]:
        assert hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest() == item["sha256"]


def test_corrupt_backup_refuses_resume_before_another_live_write(surface, monkeypatch):
    repo, home, manifest = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", "replace:1")
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")
    transaction, journal = _transaction(home, manifest)
    before = {
        item["path"]: hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        for item in journal["targets"]
    }
    backup = transaction / journal["targets"][0]["backup_path"]
    backup.write_bytes(b"corrupt")
    backup.chmod(0o600)

    with pytest.raises(migration.MigrationError, match="backup"):
        migration.apply()

    after = {
        item["path"]: hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        for item in journal["targets"]
    }
    assert after == before


def test_multiple_formal_transactions_are_refused(surface, monkeypatch):
    _, home, manifest = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", "replace:0")
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")
    transaction, _ = _transaction(home, manifest)
    clone = transaction.parent / "migration-clone"
    shutil.copytree(transaction, clone)

    with pytest.raises(migration.MigrationError, match="ambiguous"):
        migration.apply()


def test_manifest_symlink_and_target_hardlink_are_refused(surface, tmp_path):
    repo, _, _ = surface
    manifest_path = Path(os.environ["PICO_PLAN1_MANIFEST"])
    manifest_copy = manifest_path.with_name("manifest-copy.json")
    manifest_path.rename(manifest_copy)
    manifest_path.symlink_to(manifest_copy)
    with pytest.raises(migration.MigrationError, match="manifest"):
        migration.check()

    manifest_path.unlink()
    manifest_copy.rename(manifest_path)
    os.link(repo / ".pico/sessions/s0.json", tmp_path / "alias")
    with pytest.raises(migration.MigrationError, match="unsafe"):
        migration.apply()


def test_external_mutex_is_non_reentrant_and_creates_no_transaction(surface):
    _, home, manifest = surface
    backup_root = home / ".pico/backups" / manifest["repo_hash"]
    migration.ensure_private_dir(backup_root)
    mutex = backup_root / "migration.lock"
    with migration.file_lock.locked_file(mutex, require_lock=True, blocking=False):
        with pytest.raises(RuntimeError, match="reentry"):
            migration.apply()
    assert not list(backup_root.glob("migration-*"))


def test_legacy_mode_unknown_becomes_explicit_review_only():
    entry = {
        "path": "note.txt",
        "change_kind": "modified",
        "snapshot_eligible": True,
        "ineligible_reason": "",
        "before_exists": True,
        "before_blob_ref": "a" * 64,
        "before_hash": "a" * 64,
        "after_exists": True,
        "after_blob_ref": "b" * 64,
        "after_hash": "b" * 64,
        "expected_current_hash": "b" * 64,
    }

    current = migration._review_only_entry(entry)

    assert current["snapshot_eligible"] is False
    assert current["ineligible_reason"] == "mode_unknown"
    assert current["before_mode"] is None
    assert current["after_mode"] is None
    assert current["source_tool_change_ids"] == []


@pytest.mark.parametrize(
    "corruption",
    [
        "extra_top",
        "bad_name",
        "prepared_with_applied",
        "verified_incomplete",
        "unknown_applied",
        "target_set",
        "original_metadata",
        "backup_escape",
        "transformed_hash_type",
    ],
)
def test_journal_corruption_refuses_before_more_live_writes(
    surface, monkeypatch, corruption
):
    repo, home, manifest = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", "replace:0")
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")
    root, journal = _transaction(home, manifest)
    before = {
        item["path"]: hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        for item in journal["targets"]
    }
    if corruption == "extra_top":
        journal["extra"] = True
    elif corruption == "bad_name":
        journal["transaction_name"] = "migration-latest"
    elif corruption == "prepared_with_applied":
        journal["status"] = "prepared"
        journal["applied_paths"] = [journal["targets"][0]["path"]]
    elif corruption == "verified_incomplete":
        journal["status"] = "verified"
        journal["applied_paths"] = []
    elif corruption == "unknown_applied":
        journal["applied_paths"] = ["sessions/unknown.json"]
    elif corruption == "target_set":
        journal["targets"][0]["path"] = "sessions/unknown.json"
    elif corruption == "original_metadata":
        journal["targets"][0]["original"]["inode"] += 1
    elif corruption == "backup_escape":
        journal["targets"][0]["backup_path"] = "../escape"
    else:
        journal["targets"][0]["transformed_sha256"] = True
    _write_journal_direct(root, journal)

    with pytest.raises(migration.MigrationError):
        migration.apply()

    after = {
        item["path"]: hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        for item in journal["targets"]
        if (repo / ".pico" / item["path"]).is_file()
    }
    assert all(after[path] == digest for path, digest in before.items() if path in after)


def test_partial_staging_with_unowned_content_is_not_cleaned(surface, monkeypatch):
    repo, home, manifest = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", "backup:3")
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")
    backup_root = home / ".pico/backups" / manifest["repo_hash"]
    staging = next(backup_root.glob(".migration-staging-*"))
    _write(staging / "unowned", b"x")

    with pytest.raises(migration.MigrationError, match="staging"):
        migration.apply()

    assert staging.exists()
    assert all(
        hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        == item["sha256"]
        for item in manifest["entries"]
    )


def test_verify_only_drift_blocks_rollback_before_target_write(surface, monkeypatch):
    repo, _, manifest = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", "replace:2")
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")
    before = {
        item["path"]: hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        for item in manifest["entries"] if item["role"] == "transform"
    }
    verify_only = next(item for item in manifest["entries"] if item["path"].startswith("runs/"))
    path = repo / ".pico" / verify_only["path"]
    path.write_bytes(b"drift")
    path.chmod(0o600)

    with pytest.raises(migration.MigrationError, match="drift"):
        migration.rollback()

    assert before == {
        item["path"]: hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        for item in manifest["entries"] if item["role"] == "transform"
    }


def test_partial_rollback_continues_and_second_attempt_recovers(
    surface, monkeypatch
):
    _, _, _ = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", "replace:4")
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")
    real_replace = migration._replace_target
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("synthetic rollback failure")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(migration, "_replace_target", fail_once)
    with pytest.raises(migration.MigrationError, match="incomplete"):
        migration.rollback()
    monkeypatch.setattr(migration, "_replace_target", real_replace)

    migration.rollback()
    migration.verify_original()


def test_dangling_user_memory_symlink_is_refused(surface, tmp_path):
    _, home, _ = surface
    memory = home / ".pico/memory"
    memory.symlink_to(tmp_path / "missing")

    with pytest.raises(migration.MigrationError, match="memory"):
        migration.check()


def test_invalid_legacy_prepared_entry_stops_before_live_write(surface):
    repo, _, manifest = surface
    relative = "checkpoints/tool_changes/tc0.json"
    path = repo / ".pico" / relative
    record = json.loads(path.read_text())
    record["prepared_file_entries"] = [
        {
            "path": "note.txt",
            "before_exists": True,
            "before_blob_ref": "a" * 64,
            "before_hash": "a" * 64,
        }
    ]
    path.write_text(json.dumps(record) + "\n")
    path.chmod(0o600)
    _refresh_manifest_entry(manifest, relative)

    with pytest.raises(migration.MigrationError, match="transformed tool"):
        migration.apply()

    assert all(
        hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        == item["sha256"]
        for item in manifest["entries"]
    )


def test_session_checkpoint_order_and_recovery_cross_refs_are_preserved(surface):
    repo, _, _ = surface
    before = json.loads((repo / ".pico/sessions/s0.json").read_text())
    before_order = list(before["checkpoints"]["items"])

    migration.apply()

    current = json.loads((repo / ".pico/sessions/s0.json").read_text())
    assert list(current["checkpoints"]["items"]) == before_order
    for index in range(2):
        checkpoint = json.loads(
            (repo / f".pico/checkpoints/records/ck{index}.json").read_text()
        )
        tool = json.loads(
            (repo / f".pico/checkpoints/tool_changes/tc{index}.json").read_text()
        )
        assert checkpoint["tool_change_ids"] == [tool["tool_change_id"]]
        assert tool["checkpoint_id"] == checkpoint["checkpoint_id"]


def test_cross_process_mutex_busy_refuses_immediately(surface):
    _, home, manifest = surface
    backup_root = home / ".pico/backups" / manifest["repo_hash"]
    migration.ensure_private_dir(backup_root)
    mutex = backup_root / "migration.lock"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,sys,time; "
                "f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_EX); "
                "print('ready',flush=True); time.sleep(30)"
            ),
            str(mutex),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="busy"):
            migration.apply()
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert not list(backup_root.glob("migration-*"))


def test_lock_order_and_authoritative_verification_hold_both_store_locks(
    surface, monkeypatch
):
    repo, _, _ = surface
    calls = []
    real_locked = migration.file_lock.locked_file
    real_verify = migration._strict_verify_locked

    def tracked(path, **kwargs):
        calls.append(Path(path).name)
        return real_locked(path, **kwargs)

    def assert_locked(*args):
        assert migration.file_lock.lock_is_active(
            repo / ".pico/checkpoints/.checkpoint_store.lock"
        )
        assert migration.file_lock.lock_is_active(
            repo / ".pico/sessions/.session_store.lock"
        )
        return real_verify(*args)

    monkeypatch.setattr(migration.file_lock, "locked_file", tracked)
    monkeypatch.setattr(migration, "_strict_verify_locked", assert_locked)

    migration.apply()

    assert calls[:3] == [
        "migration.lock",
        ".checkpoint_store.lock",
        ".session_store.lock",
    ]
    assert not (repo / ".pico/checkpoints/.mutation.lock").exists()


def test_missing_store_lock_refuses_without_creating_any_lock(surface):
    repo, home, manifest = surface
    (repo / ".pico/sessions/.session_store.lock").unlink()

    with pytest.raises(migration.MigrationError, match="path set"):
        migration.apply()

    assert not (repo / ".pico/sessions/.session_store.lock").exists()
    assert not (repo / ".pico/checkpoints/.mutation.lock").exists()
    assert not list(
        (home / ".pico/backups" / manifest["repo_hash"]).glob("migration-*")
    )


def test_formal_and_staging_combination_is_ambiguous(surface, monkeypatch):
    _, home, manifest = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", "replace:0")
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")
    backup_root = home / ".pico/backups" / manifest["repo_hash"]
    (backup_root / (".migration-staging-" + "a" * 24)).mkdir(mode=0o700)

    with pytest.raises(migration.MigrationError, match="ambiguous"):
        migration.apply()


@pytest.mark.parametrize("swap", ["target_fifo", "parent_symlink", "owned_temp_fifo"])
def test_live_type_and_parent_swaps_fail_closed(surface, monkeypatch, tmp_path, swap):
    repo, _, _ = surface
    target = repo / ".pico/sessions/s0.json"
    if swap == "target_fifo":
        target.unlink()
        os.mkfifo(target, 0o600)
    elif swap == "parent_symlink":
        sessions = target.parent
        moved = tmp_path / "sessions"
        sessions.rename(moved)
        sessions.symlink_to(moved, target_is_directory=True)
    else:
        monkeypatch.setenv("PICO_MIGRATION_FAULT", "replace:0:after-swap")
        with pytest.raises(migration.MigrationInterrupted):
            migration.apply()
        monkeypatch.delenv("PICO_MIGRATION_FAULT")
        owned = next((repo / ".pico").rglob("*.pico-migration.*.tmp"))
        owned.unlink()
        os.mkfifo(owned, 0o600)

    with pytest.raises(migration.MigrationError):
        migration.apply()


def test_backup_parent_symlink_swap_refuses_before_live_write(
    surface, monkeypatch, tmp_path
):
    repo, home, manifest = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", "replace:0")
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")
    root, _ = _transaction(home, manifest)
    parent = root / "original/sessions"
    moved = tmp_path / "backup-sessions"
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)
    before = {
        item["path"]: hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        for item in manifest["entries"] if item["role"] == "transform"
    }

    with pytest.raises(migration.MigrationError):
        migration.apply()

    assert before == {
        item["path"]: hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        for item in manifest["entries"] if item["role"] == "transform"
    }


def test_cli_error_never_echoes_nested_duplicate_canary(surface, capsys):
    repo, _, manifest = surface
    canary = "migration-canary-secret-never-print"
    relative = "sessions/s0.json"
    path = repo / ".pico" / relative
    path.write_bytes(
        ('{"schema_version":2,"id":"s0","nested":{"key":"safe","key":"'
        + canary
        + '"}}').encode()
    )
    path.chmod(0o600)
    _refresh_manifest_entry(manifest, relative)

    with pytest.raises(SystemExit):
        migration.main(["--apply"])

    captured = capsys.readouterr()
    assert canary not in captured.out
    assert canary not in captured.err


def test_cross_reference_business_failure_rolls_back(surface):
    repo, _, manifest = surface
    relative = "checkpoints/records/ck0.json"
    path = repo / ".pico" / relative
    record = json.loads(path.read_text())
    record["tool_change_ids"] = []
    path.write_text(json.dumps(record) + "\n")
    path.chmod(0o600)
    _refresh_manifest_entry(manifest, relative)

    with pytest.raises(migration.MigrationError, match="business"):
        migration.apply()

    assert all(
        hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        == item["sha256"]
        for item in manifest["entries"]
    )


@pytest.mark.parametrize(
    "drift", ["inode", "hash", "mode", "mtime", "size", "missing"]
)
def test_each_manifest_drift_refuses_before_transaction(surface, drift):
    repo, home, manifest = surface
    item = next(item for item in manifest["entries"] if item["role"] == "transform")
    path = repo / ".pico" / item["path"]
    if drift == "inode":
        raw = path.read_bytes()
        mode = path.stat().st_mode & 0o777
        mtime = path.stat().st_mtime_ns
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(raw)
        replacement.chmod(mode)
        os.utime(replacement, ns=(mtime, mtime))
        replacement.replace(path)
    elif drift == "hash":
        raw = bytearray(path.read_bytes())
        raw[-2] = ord(" ") if raw[-2] != ord(" ") else ord("\t")
        mtime = path.stat().st_mtime_ns
        path.write_bytes(raw)
        path.chmod(0o600)
        os.utime(path, ns=(mtime, mtime))
    elif drift == "mode":
        path.chmod(0o400)
    elif drift == "mtime":
        os.utime(path, ns=(item["mtime_ns"] + 1_000_000, item["mtime_ns"] + 1_000_000))
    elif drift == "size":
        path.write_bytes(path.read_bytes() + b"x")
        path.chmod(0o600)
    else:
        path.unlink()

    with pytest.raises(migration.MigrationError):
        migration.apply()

    assert not list(
        (home / ".pico/backups" / manifest["repo_hash"]).glob("migration-*")
    )


def test_prepared_staging_backup_corruption_refuses_before_promotion(
    surface, monkeypatch
):
    repo, home, manifest = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", "prepared")
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")
    backup_root = home / ".pico/backups" / manifest["repo_hash"]
    staging = next(backup_root.glob(".migration-staging-*"))
    journal = json.loads((staging / "journal.json").read_text())
    backup = staging / journal["targets"][0]["backup_path"]
    backup.write_bytes(b"corrupt")
    backup.chmod(0o600)

    with pytest.raises(migration.MigrationError, match="backup"):
        migration.apply()

    assert not list(backup_root.glob("migration-*"))
    assert all(
        hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        == item["sha256"]
        for item in manifest["entries"]
    )


def test_backup_swap_after_journal_validation_is_caught_before_apply_write(
    surface, monkeypatch
):
    repo, home, manifest = surface
    real_validate = migration._validate_journal
    corrupted = False

    def corrupt_after_validation(root, root_identity, journal, identity, loaded_manifest, **kwargs):
        nonlocal corrupted
        result = real_validate(
            root, root_identity, journal, identity, loaded_manifest, **kwargs
        )
        if not corrupted:
            corrupted = True
            backup = root / journal["targets"][0]["backup_path"]
            backup.write_bytes(b"swapped-after-validation")
            backup.chmod(0o600)
        return result

    monkeypatch.setattr(migration, "_validate_journal", corrupt_after_validation)

    with pytest.raises(migration.MigrationError, match="backup"):
        migration.apply()

    assert all(
        hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        == item["sha256"]
        for item in manifest["entries"]
    )
    assert not list(
        (home / ".pico/backups" / manifest["repo_hash"]).glob("*.unexpected")
    )


def test_backup_swap_after_rollback_preflight_blocks_all_restore_writes(
    surface, monkeypatch
):
    repo, _, manifest = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", "replace:3")
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")
    before = {
        item["path"]: hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        for item in manifest["entries"] if item["role"] == "transform"
    }
    real_validate = migration._validate_journal
    calls = 0

    def corrupt_inside_rollback(root, root_identity, journal, identity, loaded_manifest, **kwargs):
        nonlocal calls
        result = real_validate(
            root, root_identity, journal, identity, loaded_manifest, **kwargs
        )
        calls += 1
        if calls == 2:
            backup = root / journal["targets"][0]["backup_path"]
            backup.write_bytes(b"rollback-backup-swap")
            backup.chmod(0o600)
        return result

    monkeypatch.setattr(migration, "_validate_journal", corrupt_inside_rollback)

    with pytest.raises(migration.MigrationError, match="backup"):
        migration.rollback()

    assert before == {
        item["path"]: hashlib.sha256((repo / ".pico" / item["path"]).read_bytes()).hexdigest()
        for item in manifest["entries"] if item["role"] == "transform"
    }


def test_pre_exchange_temp_inode_swap_atomically_restores_original_live_target(
    surface, monkeypatch
):
    repo, _, manifest = surface
    original = {
        item["path"]: (repo / ".pico" / item["path"]).read_bytes()
        for item in manifest["entries"] if item["role"] == "transform"
    }
    real_swap = migration._rename_swap
    swapped_temp = False

    def replace_temp_inode(parent, first, second):
        nonlocal swapped_temp
        if not swapped_temp:
            swapped_temp = True
            os.unlink(first, dir_fd=parent)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(first, flags, 0o600, dir_fd=parent)
            try:
                os.write(descriptor, b"attacker-temp")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return real_swap(parent, first, second)

    monkeypatch.setattr(migration, "_rename_swap", replace_temp_inode)

    with pytest.raises(migration.MigrationError):
        migration.apply()

    assert swapped_temp is True
    assert all(
        (repo / ".pico" / relative).read_bytes() == raw
        for relative, raw in original.items()
    )


def test_direct_sigkill_owned_live_temp_fixture_reconciles_to_verified(
    surface, monkeypatch
):
    repo, home, manifest = surface
    monkeypatch.setenv("PICO_MIGRATION_FAULT", "replace:0")
    with pytest.raises(migration.MigrationInterrupted):
        migration.apply()
    monkeypatch.delenv("PICO_MIGRATION_FAULT")
    root, journal = _transaction(home, manifest)
    target = journal["targets"][0]
    live = repo / ".pico" / target["path"]
    backup = root / target["backup_path"]
    leftover = live.with_name(
        f".{live.name}.pico-migration.{'a' * 24}.tmp"
    )
    leftover.write_bytes(backup.read_bytes())
    leftover.chmod(0o600)
    assert hashlib.sha256(live.read_bytes()).hexdigest() == target["transformed_sha256"]
    assert hashlib.sha256(leftover.read_bytes()).hexdigest() == target["original"]["sha256"]

    migration.apply()

    assert not leftover.exists()
    _, verified = _transaction(home, manifest)
    assert verified["status"] == "verified"
