import json
import os

import pytest

import pony.state.session_store as session_store_module
from pony.agent.messages import make_tool_pair
from pony.state.session_store import (
    LEGACY_JSONL_SESSION_FORMAT_VERSION,
    PREVIOUS_SESSION_FORMAT_VERSION,
    SESSION_FORMAT_VERSION,
    SessionFormatError,
    SessionMigrationRequired,
    SessionStore,
    UnsupportedLegacyEntry,
)


def _session(workspace, session_id="permission"):
    return {
        "record_type": "session",
        "format_version": SESSION_FORMAT_VERSION,
        "id": session_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "workspace_root": str(workspace),
        "messages": [],
        "working_memory": {"task_summary": "", "recent_files": []},
        "memory": {"file_summaries": {}},
        "recently_recalled": [],
        "checkpoints": {"current_id": "", "items": {}},
        "resume_state": {},
        "recovery": {"current_checkpoint_id": ""},
        "runtime_identity": {},
        "permission_mode": "default",
    }


def _legacy_plan():
    return {
        "goal": "Historical plan",
        "items": [{"id": "old", "text": "Old step", "status": "pending"}],
    }


def _rewrite_as_v3(path, *, mode="act", plan=None):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row["format_version"] = PREVIOUS_SESSION_FORMAT_VERSION
        if row.get("type") == "session_info":
            row["data"]["set"]["format_version"] = PREVIOUS_SESSION_FORMAT_VERSION
    parent_id = rows[-1]["id"]
    if mode != "act":
        rows.append(
            {
                "record_type": "session_entry",
                "format_version": PREVIOUS_SESSION_FORMAT_VERSION,
                "id": "a" * 24,
                "parent_id": parent_id,
                "timestamp": "2026-01-01T00:00:01+00:00",
                "type": "workflow_mode_change",
                "data": {"mode": mode},
            }
        )
        parent_id = rows[-1]["id"]
    if plan is not None:
        rows.append(
            {
                "record_type": "session_entry",
                "format_version": PREVIOUS_SESSION_FORMAT_VERSION,
                "id": "b" * 24,
                "parent_id": parent_id,
                "timestamp": "2026-01-01T00:00:02+00:00",
                "type": "plan_update",
                "data": {"plan": plan},
            }
        )
    raw = b"".join(
        (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
        for row in rows
    )
    path.write_bytes(raw)
    return rows, raw


def _rewrite_as_v2(path, *, model_change=False):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row["format_version"] = LEGACY_JSONL_SESSION_FORMAT_VERSION
        if row.get("type") == "session_info":
            row["data"]["set"]["format_version"] = LEGACY_JSONL_SESSION_FORMAT_VERSION
    if model_change:
        rows.append(
            {
                "record_type": "session_entry",
                "format_version": LEGACY_JSONL_SESSION_FORMAT_VERSION,
                "id": "c" * 24,
                "parent_id": rows[-1]["id"],
                "timestamp": "2026-01-01T00:00:03+00:00",
                "type": "model_change",
                "data": {},
            }
        )
    raw = b"".join(
        (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
        for row in rows
    )
    path.write_bytes(raw)
    return rows, raw


def _legacy_source(store, workspace, session_id, version):
    if version == LEGACY_JSONL_SESSION_FORMAT_VERSION:
        path = store.save(_session(workspace, session_id))
        _rewrite_as_v2(path)
        return path
    if version == PREVIOUS_SESSION_FORMAT_VERSION:
        path = store.save(_session(workspace, session_id))
        _rewrite_as_v3(path, mode="review", plan=_legacy_plan())
        return path
    payload = _session(workspace, session_id)
    payload["format_version"] = 1
    payload.pop("permission_mode")
    path = store.legacy_path(session_id)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    store.lock_path.touch(mode=0o600)
    return path


def test_current_permission_control_projects_and_forks(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    store.save(_session(tmp_path))
    base_leaf = store.load_tree("permission").leaf_id

    changed = store.set_permission_mode("permission", "plan")

    assert changed["type"] == "permission_mode_change"
    assert store.load("permission")["permission_mode"] == "plan"
    store.fork("permission", base_leaf)
    assert store.load("permission")["permission_mode"] == "default"


def test_current_permission_requires_valid_explicit_control(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path)
    store.save(session)

    with pytest.raises(ValueError, match="invalid permission mode"):
        store.set_permission_mode("permission", "auto")
    session["permission_mode"] = "plan"
    with pytest.raises(SessionFormatError, match="explicit control"):
        store.save(session)


@pytest.mark.parametrize(
    ("legacy_mode", "permission_mode"),
    (("act", "default"), ("plan", "plan"), ("review", "plan")),
)
def test_v3_inspection_is_read_only_and_resume_maps_permission(
    tmp_path,
    legacy_mode,
    permission_mode,
):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = store.save(_session(tmp_path, f"legacy-{legacy_mode}"))
    old_rows, old_raw = _rewrite_as_v3(
        path,
        mode=legacy_mode,
        plan=_legacy_plan(),
    )
    before = path.stat()

    storage, projection, tree = store.inspect_readonly(f"legacy-{legacy_mode}")

    after = path.stat()
    assert storage == "legacy_jsonl"
    assert projection["workflow_mode"] == legacy_mode
    assert projection["active_plan"] == _legacy_plan()
    assert tree.leaf_id == old_rows[-1]["id"]
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert not (store.root / "legacy-backups").exists()
    with pytest.raises(SessionMigrationRequired):
        store.load(f"legacy-{legacy_mode}")

    migrated = store.load_for_resume(f"legacy-{legacy_mode}")
    new_rows = [json.loads(line) for line in path.read_text().splitlines()]
    backup = next((store.root / "legacy-backups").glob("*.jsonl"))
    assert migrated["permission_mode"] == permission_mode
    assert "workflow_mode" not in migrated
    assert "active_plan" not in migrated
    assert [row["id"] for row in new_rows] == [row["id"] for row in old_rows]
    assert [row.get("parent_id") for row in new_rows] == [
        row.get("parent_id") for row in old_rows
    ]
    assert any(
        row.get("type") == "migration"
        and row["data"].get("legacy_control", {}).get("plan") == _legacy_plan()
        for row in new_rows
    )
    assert backup.read_bytes() == old_raw


def test_v3_update_plan_history_does_not_project_into_v4(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session_id = "legacy-tool-plan"
    store.save(_session(tmp_path, session_id))
    pair = make_tool_pair(
        name="update_plan",
        arguments={"plan_json": json.dumps(_legacy_plan())},
        tool_use_id="legacy-plan",
        result_content="updated",
        created_at="2026-01-01T00:00:01+00:00",
        tool_status="ok",
        effect_class="session_state",
    )
    store.append_messages(session_id, pair)
    _rewrite_as_v3(store.path(session_id), mode="review")

    assert store.inspect_readonly(session_id)[1]["active_plan"] == _legacy_plan()
    migrated = store.load_for_resume(session_id)

    assert migrated["permission_mode"] == "plan"
    assert migrated["messages"] == list(pair)
    assert "active_plan" not in migrated


def test_v2_inspection_is_read_only_and_resume_defaults_permission(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = store.save(_session(tmp_path, "legacy-v2"))
    old_rows, old_raw = _rewrite_as_v2(path)
    before = path.stat()

    storage, projection, tree = store.inspect_readonly("legacy-v2")

    after = path.stat()
    assert storage == "legacy_jsonl"
    assert projection["format_version"] == LEGACY_JSONL_SESSION_FORMAT_VERSION
    assert tree.leaf_id == old_rows[-1]["id"]
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert not (store.root / "legacy-backups").exists()

    migrated = store.load_for_resume("legacy-v2")
    new_rows = [json.loads(line) for line in path.read_text().splitlines()]
    backup = next((store.root / "legacy-backups").glob("*.jsonl"))
    assert migrated["permission_mode"] == "default"
    assert [row["id"] for row in new_rows] == [row["id"] for row in old_rows]
    assert [row.get("parent_id") for row in new_rows] == [
        row.get("parent_id") for row in old_rows
    ]
    assert backup.read_bytes() == old_raw


def test_v2_model_change_fails_before_writing_migration_artifacts(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = store.save(_session(tmp_path, "unsupported-v2"))
    _, original = _rewrite_as_v2(path, model_change=True)

    with pytest.raises(UnsupportedLegacyEntry, match="model_change"):
        store.load_for_resume("unsupported-v2")

    assert path.read_bytes() == original
    assert not store.candidate_path("unsupported-v2").exists()
    assert not (store.root / "legacy-backups").exists()


def test_v1_inspection_is_read_only_and_creates_no_migration_artifacts(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    source = _legacy_source(store, tmp_path, "legacy-readonly", 1)
    before = source.stat()

    storage, projection, tree = store.inspect_readonly("legacy-readonly")

    after = source.stat()
    assert storage == "legacy"
    assert projection["format_version"] == 1
    assert tree is None
    assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_mode,
    )
    assert not store.candidate_path("legacy-readonly").exists()
    assert not (store.root / "legacy-backups").exists()


def test_v3_tail_repair_requires_resume_without_writing(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = _legacy_source(store, tmp_path, "tail-v3", PREVIOUS_SESSION_FORMAT_VERSION)
    path.write_bytes(path.read_bytes() + b'{"incomplete":')
    original = path.read_bytes()

    with pytest.raises(SessionMigrationRequired):
        store.repair_tail("tail-v3")

    assert path.read_bytes() == original


def test_v2_tail_repair_requires_resume_without_writing(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = _legacy_source(
        store,
        tmp_path,
        "tail-v2",
        LEGACY_JSONL_SESSION_FORMAT_VERSION,
    )
    path.write_bytes(path.read_bytes() + b'{"incomplete":')
    original = path.read_bytes()

    with pytest.raises(SessionMigrationRequired):
        store.repair_tail("tail-v2")

    assert path.read_bytes() == original


def test_v3_publish_failure_keeps_source_and_resume_is_retryable(
    tmp_path,
    monkeypatch,
):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = _legacy_source(store, tmp_path, "retry-v3", PREVIOUS_SESSION_FORMAT_VERSION)
    original = path.read_bytes()
    replace = os.replace

    def fail_candidate_publish(source, destination, **kwargs):
        if str(source).endswith(".jsonl.candidate"):
            raise OSError("candidate publish failed")
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(session_store_module.os, "replace", fail_candidate_publish)
    with pytest.raises(OSError, match="candidate publish failed"):
        store.load_for_resume("retry-v3")
    assert path.read_bytes() == original

    monkeypatch.setattr(session_store_module.os, "replace", replace)
    assert store.load_for_resume("retry-v3")["format_version"] == SESSION_FORMAT_VERSION


def test_v2_publish_failure_keeps_source_and_resume_is_retryable(
    tmp_path,
    monkeypatch,
):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = _legacy_source(
        store,
        tmp_path,
        "retry-v2",
        LEGACY_JSONL_SESSION_FORMAT_VERSION,
    )
    original = path.read_bytes()
    replace = os.replace

    def fail_candidate_publish(source, destination, **kwargs):
        if str(source).endswith(".jsonl.candidate"):
            raise OSError("candidate publish failed")
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(session_store_module.os, "replace", fail_candidate_publish)
    with pytest.raises(OSError, match="candidate publish failed"):
        store.load_for_resume("retry-v2")
    assert path.read_bytes() == original

    monkeypatch.setattr(session_store_module.os, "replace", replace)
    assert store.load_for_resume("retry-v2")["format_version"] == SESSION_FORMAT_VERSION


@pytest.mark.parametrize(
    "version",
    (1, LEGACY_JSONL_SESSION_FORMAT_VERSION, PREVIOUS_SESSION_FORMAT_VERSION),
)
def test_migration_rejects_source_changed_during_read(tmp_path, monkeypatch, version):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session_id = f"source-race-v{version}"
    source = _legacy_source(store, tmp_path, session_id, version)
    read = session_store_module.read_private_bytes
    source_reads = 0

    def change_after_read(path, **kwargs):
        nonlocal source_reads
        raw = read(path, **kwargs)
        if path == source:
            source_reads += 1
            if source_reads == (1 if version == 1 else 2):
                source.write_bytes(raw + b" ")
        return raw

    monkeypatch.setattr(session_store_module, "read_private_bytes", change_after_read)
    with pytest.raises(SessionFormatError, match="changed during migration"):
        store.load_for_resume(session_id)
    assert not store.candidate_path(session_id).exists()
    assert not (store.root / "legacy-backups").exists()


@pytest.mark.parametrize(
    "version",
    (1, LEGACY_JSONL_SESSION_FORMAT_VERSION, PREVIOUS_SESSION_FORMAT_VERSION),
)
def test_migration_rechecks_candidate_identity_before_replace(
    tmp_path,
    monkeypatch,
    version,
):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session_id = f"candidate-race-v{version}"
    source = _legacy_source(store, tmp_path, session_id, version)
    original = source.read_bytes()
    candidate = store.candidate_path(session_id)
    signature = session_store_module.private_file_signature
    candidate_checks = 0

    def changed_signature(path, **kwargs):
        nonlocal candidate_checks
        value = signature(path, **kwargs)
        if path == candidate:
            candidate_checks += 1
            if candidate_checks == 3:
                return (*value[:3], value[3] + 1, *value[4:])
        return value

    monkeypatch.setattr(session_store_module, "private_file_signature", changed_signature)
    with pytest.raises(SessionFormatError, match="candidate changed"):
        store.load_for_resume(session_id)
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "version",
    (1, LEGACY_JSONL_SESSION_FORMAT_VERSION, PREVIOUS_SESSION_FORMAT_VERSION),
)
def test_migration_rejects_mismatched_existing_backup(tmp_path, monkeypatch, version):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session_id = f"backup-race-v{version}"
    source = _legacy_source(store, tmp_path, session_id, version)
    original = source.read_bytes()
    replace = os.replace

    def fail_candidate_publish(source_name, destination, **kwargs):
        if str(source_name).endswith(".jsonl.candidate"):
            raise OSError("candidate publish failed")
        return replace(source_name, destination, **kwargs)

    monkeypatch.setattr(session_store_module.os, "replace", fail_candidate_publish)
    with pytest.raises(OSError, match="candidate publish failed"):
        store.load_for_resume(session_id)
    backup = next((store.root / "legacy-backups").iterdir())
    backup.write_bytes(b"wrong backup")
    backup.chmod(0o600)

    monkeypatch.setattr(session_store_module.os, "replace", replace)
    with pytest.raises(SessionFormatError, match="backup changed"):
        store.load_for_resume(session_id)
    assert source.read_bytes() == original
