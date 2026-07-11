import copy
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

import pico.session_store as session_store_module
from pico import security as security_module
from pico import FakeModelClient, Pico, WorkspaceContext
from pico.messages import validate_messages
from pico.security import redact_artifact
from pico.session_store import (
    SessionMigrationError,
    SessionStore,
    migrate_session_to_v3,
)


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / ".pico" / "sessions")


def _v1_session(tmp_path):
    return {
        "id": "s1",
        "created_at": "2026-01-01T00:00:00Z",
        "workspace_root": str(tmp_path),
        "history": [
            {"role": "user", "content": "hi", "created_at": "2026-01-01T00:00:01Z"},
            {"role": "assistant", "content": "hello", "created_at": "2026-01-01T00:00:02Z"},
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": "a.py"},
                "content": "file content",
                "created_at": "2026-01-01T00:00:03Z",
            },
        ],
        "working_memory": {"task_summary": "", "recent_files": []},
    }


def test_migrator_converts_history_to_messages(store, tmp_path):
    v1 = _v1_session(tmp_path)
    store.save(v1)
    # 手动修改磁盘保留 v1 形状，然后再 load
    session_path = store.path_for(v1["id"])
    session_path.write_text(json.dumps(v1), encoding="utf-8")

    loaded = store.load("s1")
    assert loaded["schema_version"] == 3
    assert "history" not in loaded
    assert "messages" in loaded
    msgs = loaded["messages"]
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    # 老 tool 事件被拆成两条：assistant tool_use + user tool_result
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"][0]["type"] == "tool_use"
    tool_use_id = msgs[2]["content"][0]["id"]
    assert msgs[3]["role"] == "user"
    assert msgs[3]["content"][0]["type"] == "tool_result"
    assert msgs[3]["content"][0]["tool_use_id"] == tool_use_id


def test_migrator_writes_backup(store, tmp_path):
    v1 = _v1_session(tmp_path)
    session_path = store.path_for(v1["id"])
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps(v1), encoding="utf-8")

    store.load("s1")

    backup_dir = session_path.parent / "backup"
    backups = list(backup_dir.glob("s1.v1.*.json"))
    assert len(backups) == 1
    backup_body = json.loads(backups[0].read_text(encoding="utf-8"))
    assert "history" in backup_body


def test_migration_backup_rejects_hardlink_before_writing_raw_bytes(
    store,
    tmp_path,
    monkeypatch,
):
    secret = "raw-migration-secret-123456789"
    session = _v1_session(tmp_path)
    session["history"][0]["content"] = secret
    session_path = store.path_for(session["id"])
    session_path.write_text(json.dumps(session), encoding="utf-8")
    alias = tmp_path / "outside-backup-alias.json"
    real_fsync = security_module.os.fsync
    linked = False

    def hardlink_after_write(descriptor):
        nonlocal linked
        real_fsync(descriptor)
        temps = list((store.root / "backup").glob(".*.tmp"))
        if not linked and temps:
            linked = True
            os.link(temps[0], alias)

    monkeypatch.setattr(security_module.os, "fsync", hardlink_after_write)

    with pytest.raises(ValueError, match="backup|link|changed"):
        store.load("s1")

    assert alias.read_bytes() == b""
    assert secret.encode("utf-8") not in alias.read_bytes()
    assert not list((store.root / "backup").glob("*.json"))


def test_v3_load_sanitizes_before_return_and_rewrites_once(tmp_path):
    secret = "github_pat_A123456789012345678901234567890"
    store = SessionStore(
        tmp_path / ".pico" / "sessions",
        redactor=lambda value: redact_artifact(value, env={}),
    )
    session = {
        "id": "resume-safe",
        "schema_version": 3,
        "messages": [{"role": "user", "content": secret, "_pico_meta": {}}],
    }
    path = store.path("resume-safe")
    original = json.dumps(session).encode("utf-8")
    path.write_bytes(original)

    loaded = store.load("resume-safe")

    assert secret not in json.dumps(loaded)
    assert secret not in path.read_text(encoding="utf-8")
    backups = list((store.root / "backup").glob("resume-safe*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    if os.name == "posix":
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        assert stat.S_IMODE((store.root / "backup").stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600
    store.load("resume-safe")
    assert len(list((store.root / "backup").glob("resume-safe*.json"))) == 1


def test_v3_load_rewrites_shadowed_duplicate_secret_once(tmp_path):
    secret = "github_pat_A123456789012345678901234567890"
    store = SessionStore(tmp_path / ".pico" / "sessions")
    path = store.path("duplicate-safe")
    original = (
        b'{"id":"duplicate-safe","schema_version":3,"messages":['
        b'{"role":"user","content":"'
        + secret.encode("utf-8")
        + b'","content":"safe","_pico_meta":{}}]}'
    )
    path.write_bytes(original)

    loaded = store.load("duplicate-safe")

    assert loaded["messages"][0]["content"] == "safe"
    assert secret not in path.read_text(encoding="utf-8")
    backups = list((store.root / "backup").glob("duplicate-safe*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    store.load("duplicate-safe")
    assert len(list((store.root / "backup").glob("duplicate-safe*.json"))) == 1


def test_migrator_upgrades_v2_once(store, tmp_path):
    v2 = {
        "id": "s2",
        "workspace_root": str(tmp_path),
        "messages": [{"role": "user", "content": "hi"}],
        "schema_version": 2,
    }
    session_path = store.path_for("s2")
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps(v2), encoding="utf-8")

    loaded = store.load("s2")
    assert loaded["schema_version"] == 3
    backup_dir = session_path.parent / "backup"
    backups = list(backup_dir.glob("s2.v2.*.json"))
    assert len(backups) == 1


def test_backup_uses_nanosecond_precision_in_filename(tmp_path):
    """Task A4: backup filename should carry nanosecond precision."""
    import json
    import re
    from pico.session_store import SessionStore

    store = SessionStore(tmp_path / ".pico" / "sessions")

    v1 = {
        "id": "s1",
        "created_at": "2026-01-01T00:00:00Z",
        "workspace_root": str(tmp_path),
        "history": [{"role": "user", "content": "hi", "created_at": "2026-01-01T00:00:01Z"}],
    }
    p = store.path_for("s1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(v1), encoding="utf-8")
    store.load("s1")

    backup_dir = p.parent / "backup"
    backups = list(backup_dir.glob("s1.v1.*.json"))
    assert len(backups) == 1
    # Nanosecond timestamps are 19 digits (10^19 ns ≈ 316 years since epoch);
    # second timestamps were 10 digits. Assert the numeric suffix has ≥ 15 digits.
    match = re.match(r"s1\.v1\.(\d+)\.[a-f0-9]+\.json$", backups[0].name)
    assert match is not None
    assert len(match.group(1)) >= 15, f"Expected nanosecond precision, got {match.group(1)!r}"


def test_migrator_preserves_created_at_and_tool_use_id():
    """Task E8: migrator must carry _pico_meta.created_at across every
    message type and set _pico_meta.tool_use_id on both halves of tool pairs."""
    v1 = {
        "id": "s",
        "schema_version": 1,
        "history": [
            {"role": "user", "content": "hi", "created_at": "2026-04-01T00:00:00Z"},
            {"role": "tool", "name": "read_file", "args": {"path": "x"}, "content": "y", "created_at": "2026-04-01T00:00:01Z"},
            {"role": "assistant", "content": "done", "created_at": "2026-04-01T00:00:02Z"},
        ],
    }
    migrated = migrate_session_to_v3(v1)
    msgs = migrated["messages"]
    # user
    assert msgs[0]["_pico_meta"]["created_at"] == "2026-04-01T00:00:00Z"
    # assistant tool_use half
    assert msgs[1]["_pico_meta"]["created_at"] == "2026-04-01T00:00:01Z"
    tool_use_id = msgs[1]["_pico_meta"]["tool_use_id"]
    assert tool_use_id
    # user tool_result half — must share tool_use_id
    assert msgs[2]["_pico_meta"]["tool_use_id"] == tool_use_id
    assert msgs[2]["_pico_meta"]["created_at"] == "2026-04-01T00:00:01Z"
    # final assistant
    assert msgs[3]["_pico_meta"]["created_at"] == "2026-04-01T00:00:02Z"


def test_migrator_normalizes_v2_to_v3_without_mutating_input():
    v2 = {
        "id": "s",
        "schema_version": 2,
        "messages": [
            {"role": "user", "content": "hi", "_pico_meta": {"created_at": "x"}},
        ],
    }
    before = copy.deepcopy(v2)
    result = migrate_session_to_v3(v2)
    assert result["schema_version"] == 3
    assert result["messages"] == v2["messages"]
    assert v2 == before


def _valid_v2_messages():
    return [
        {"role": "user", "content": "q", "_pico_meta": {"created_at": "t1"}},
        {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "toolu_1",
                "name": "read_file",
                "input": {"path": "a.py"},
            }],
            "_pico_meta": {"created_at": "t2"},
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": "body",
            }],
            "_pico_meta": {"created_at": "t2"},
        },
        {
            "role": "assistant",
            "content": "done",
            "_pico_meta": {"created_at": "t3"},
        },
    ]


def test_v2_prefers_valid_nonempty_messages_and_preserves_nontranscript_state():
    source = {
        "id": "s2",
        "schema_version": 2,
        "messages": _valid_v2_messages(),
        "history": [{"role": "user", "content": "stale mirror"}],
        "working_memory": {"task_summary": "goal", "recent_files": ["a.py"]},
        "memory": {"file_summaries": {"a.py": {"summary": "fact"}}},
        "recently_recalled": ["note"],
        "checkpoints": {"current_id": "c1", "items": {"c1": {}}},
        "runtime_identity": {"workspace_fingerprint": "fp"},
        "resume_state": {"status": "full-valid"},
        "recovery": {"current_checkpoint_id": "r1"},
    }
    before = copy.deepcopy(source)
    migrated = migrate_session_to_v3(source)
    assert source == before
    assert migrated["schema_version"] == 3
    assert "history" not in migrated
    assert migrated["messages"] == before["messages"]
    for key in (
        "working_memory",
        "memory",
        "recently_recalled",
        "checkpoints",
        "runtime_identity",
        "resume_state",
        "recovery",
    ):
        assert migrated[key] == before[key]
    validate_messages(migrated["messages"], require_meta=True)


def test_v2_empty_messages_rebuilds_from_nonempty_history():
    migrated = migrate_session_to_v3({
        "id": "s2",
        "schema_version": 2,
        "messages": [],
        "history": [
            {"role": "user", "content": "q", "created_at": "t1"},
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": "a.py"},
                "content": "body",
                "created_at": "t2",
            },
            {"role": "assistant", "content": "done", "created_at": "t3"},
        ],
    })
    assert [message["role"] for message in migrated["messages"]] == [
        "user", "assistant", "user", "assistant"
    ]
    validate_messages(migrated["messages"], require_meta=True)


def test_v2_invalid_messages_recovers_from_valid_history():
    migrated = migrate_session_to_v3({
        "id": "s2",
        "schema_version": 2,
        "messages": [{
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "orphan",
                "name": "x",
                "input": {},
            }],
        }],
        "history": [{"role": "user", "content": "recover me", "created_at": "t"}],
    })
    assert migrated["messages"][0]["content"] == "recover me"


def test_unknown_history_role_fails_without_mutating_input():
    source = {
        "id": "bad",
        "schema_version": 1,
        "history": [{"role": "runtime", "content": "do not skip"}],
    }
    before = copy.deepcopy(source)
    with pytest.raises(SessionMigrationError, match="unknown history role"):
        migrate_session_to_v3(source)
    assert source == before


def test_unknown_history_role_error_omits_hostile_value():
    canary = "github_pat_A123456789012345678901234567890"

    with pytest.raises(SessionMigrationError) as caught:
        migrate_session_to_v3({
            "id": "bad",
            "schema_version": 1,
            "history": [{"role": canary, "content": "do not expose"}],
        })

    assert str(caught.value) == "unknown history role"
    assert canary not in str(caught.value)


def test_empty_v1_history_migrates_to_empty_v3_messages():
    migrated = migrate_session_to_v3({
        "id": "empty",
        "schema_version": 1,
        "history": [],
    })
    assert migrated["schema_version"] == 3
    assert migrated["messages"] == []
    assert "history" not in migrated


def test_v1_history_is_authoritative_over_stray_messages():
    migrated = migrate_session_to_v3({
        "id": "v1",
        "schema_version": 1,
        "messages": [{
            "role": "user",
            "content": "stray",
            "_pico_meta": {},
        }],
        "history": [{
            "role": "user",
            "content": "authoritative",
            "created_at": "t",
        }],
    })
    assert migrated["messages"][0]["content"] == "authoritative"


@pytest.mark.parametrize("schema_version", [None, True, "", 1.5, float("inf")])
def test_invalid_schema_versions_raise_session_migration_error(schema_version):
    with pytest.raises(SessionMigrationError, match="session schema version"):
        migrate_session_to_v3({
            "id": "bad-version",
            "schema_version": schema_version,
            "history": [],
        })


def test_invalid_schema_error_chain_omits_hostile_value():
    canary = "github_pat_A123456789012345678901234567890"

    with pytest.raises(SessionMigrationError) as caught:
        migrate_session_to_v3({"schema_version": canary})

    assert str(caught.value) == "invalid session schema version"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_unhashable_history_role_raises_session_migration_error():
    with pytest.raises(SessionMigrationError, match="unknown history role"):
        migrate_session_to_v3({
            "id": "bad-role",
            "schema_version": 1,
            "history": [{"role": [], "content": "x"}],
        })


def test_v3_is_validated_and_returned_without_history():
    source = {"id": "s3", "schema_version": 3, "messages": _valid_v2_messages()}
    assert migrate_session_to_v3(source) == source


@pytest.mark.parametrize("schema_version", ["3", 3.0])
def test_v3_requires_an_integer_schema_version(schema_version):
    with pytest.raises(SessionMigrationError, match="^invalid session schema version$"):
        migrate_session_to_v3({
            "id": "s3",
            "schema_version": schema_version,
            "messages": [],
        })


def test_load_migrates_v2_to_v3_and_backup_is_original_bytes(store):
    path = store.path_for("s2")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({
        "id": "s2",
        "schema_version": 2,
        "messages": [{"role": "user", "content": "q"}],
        "history": [{"role": "user", "content": "q"}],
    }).encode("utf-8")
    path.write_bytes(original)

    loaded = store.load("s2")

    assert loaded["schema_version"] == 3
    assert "history" not in loaded
    backups = list((path.parent / "backup").glob("s2.v2.*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_v3_load_is_idempotent_without_write_or_backup(store):
    session = {
        "id": "s3",
        "schema_version": 3,
        "messages": [{"role": "user", "content": "q", "_pico_meta": {}}],
    }
    path = store.path_for("s3")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session), encoding="utf-8")
    before = path.stat().st_mtime_ns

    assert store.load("s3") == session
    assert store.load("s3") == session
    assert path.stat().st_mtime_ns == before
    backup_dir = path.parent / "backup"
    assert not backup_dir.exists() or not list(backup_dir.iterdir())


def test_replace_failure_preserves_original_and_may_leave_backup(store, monkeypatch):
    path = store.path_for("s2")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({
        "id": "s2",
        "schema_version": 2,
        "messages": [{"role": "user", "content": "q"}],
    }).encode("utf-8")
    path.write_bytes(original)
    original_replace = security_module.os.replace

    def fail_target_replace(source, target, **kwargs):
        if Path(target).name == path.name:
            raise OSError("replace failed")
        return original_replace(source, target, **kwargs)

    monkeypatch.setattr(security_module.os, "replace", fail_target_replace)

    with pytest.raises(OSError, match="replace failed"):
        store.load("s2")

    assert path.read_bytes() == original
    backups = list((path.parent / "backup").glob("s2.v2.*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))


def test_migration_error_preserves_original_session_bytes(store):
    path = store.path_for("bad")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({
        "id": "bad",
        "schema_version": 1,
        "history": [{"role": "runtime", "content": "invalid"}],
    }).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(SessionMigrationError, match="unknown history role"):
        store.load("bad")

    assert path.read_bytes() == original


@pytest.mark.parametrize("session_id", ["", "../x", "a/b", ".", "..", "x\\y"])
def test_session_id_must_be_basename_safe(store, session_id):
    with pytest.raises(ValueError, match="invalid session id"):
        store.path_for(session_id)


def test_invalid_session_id_error_omits_hostile_value(store):
    canary = "github_pat_A123456789012345678901234567890"

    with pytest.raises(ValueError) as caught:
        store.path_for(f"../{canary}")

    assert str(caught.value) == "invalid session id"
    assert canary not in str(caught.value)


def test_load_uses_one_lock_and_never_calls_public_save(store, monkeypatch):
    calls = []

    @contextmanager
    def fake_lock(path):
        calls.append(Path(path).name)
        yield

    def fail_save(*args, **kwargs):
        raise AssertionError("load must not call public save")

    path = store.path_for("s2")
    path.write_text(json.dumps({
        "id": "s2",
        "schema_version": 2,
        "messages": [{"role": "user", "content": "q"}],
    }), encoding="utf-8")
    monkeypatch.setattr(session_store_module.file_lock, "locked_file", fake_lock)
    monkeypatch.setattr(store, "save", fail_save)

    assert store.load("s2")["schema_version"] == 3
    assert calls == [".session_store.lock"]


def test_v3_save_strips_transitional_history_without_mutating_caller(store):
    session = {
        "id": "s3",
        "schema_version": 3,
        "history": [{"role": "user", "content": "legacy"}],
        "messages": [{"role": "user", "content": "q", "_pico_meta": {}}],
    }

    store.save(session)

    on_disk = json.loads(store.path_for("s3").read_text(encoding="utf-8"))
    assert "history" not in on_disk
    assert session["history"][0]["content"] == "legacy"


def test_migration_rejects_corrupting_redactor_before_backup_or_replace(tmp_path):
    store = SessionStore(
        tmp_path / ".pico" / "sessions",
        redactor=lambda value: {**value, "messages": "not messages"},
    )
    path = store.path_for("s2")
    original = json.dumps({
        "id": "s2",
        "schema_version": 2,
        "messages": [{"role": "user", "content": "q", "_pico_meta": {}}],
        "history": [],
    }).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(SessionMigrationError, match="messages"):
        store.load("s2")

    assert path.read_bytes() == original
    assert not (path.parent / "backup").exists()


@pytest.mark.parametrize("redactor_output", [[], "not a session"])
def test_migration_rejects_non_mapping_redactor_before_backup(tmp_path, redactor_output):
    store = SessionStore(
        tmp_path / ".pico" / "sessions",
        redactor=lambda value: redactor_output,
    )
    path = store.path_for("s2")
    original = json.dumps({
        "id": "s2",
        "schema_version": 2,
        "messages": [{"role": "user", "content": "q", "_pico_meta": {}}],
        "history": [],
    }).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(SessionMigrationError, match="session payload"):
        store.load("s2")

    assert path.read_bytes() == original
    assert not (path.parent / "backup").exists()


def test_save_creates_no_temp_file_when_json_encoding_fails(store, monkeypatch):
    def fail_dump(*args, **kwargs):
        raise OSError("encode failed")

    monkeypatch.setattr(session_store_module.json, "dumps", fail_dump)

    with pytest.raises(OSError, match="encode failed"):
        store.save({
            "id": "s3",
            "schema_version": 3,
            "messages": [{"role": "user", "content": "q", "_pico_meta": {}}],
        })

    assert not list(store.root.glob("*.tmp"))


def test_load_rejects_mismatched_body_id_without_changing_bytes(store):
    path = store.path_for("requested")
    original = json.dumps({
        "id": "other",
        "schema_version": 3,
        "messages": [],
    }).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(SessionMigrationError, match="session id does not match file name"):
        store.load("requested")

    assert path.read_bytes() == original


def test_load_rejects_infinite_schema_version_without_changing_bytes(store):
    path = store.path_for("infinite")
    original = b'{"id":"infinite","schema_version":Infinity,"messages":[]}'
    path.write_bytes(original)

    with pytest.raises(SessionMigrationError, match="invalid session schema version"):
        store.load("infinite")

    assert path.read_bytes() == original


def test_load_rejects_string_v3_schema_version_without_changing_bytes(store):
    path = store.path_for("s3")
    original = json.dumps({
        "id": "s3",
        "schema_version": "3",
        "messages": [],
    }).encode("utf-8")
    path.write_bytes(original)

    with pytest.raises(SessionMigrationError, match="invalid session schema version"):
        store.load("s3")

    assert path.read_bytes() == original


def test_v2_empty_messages_rejects_non_list_history_without_mutating_input():
    source = {
        "id": "s2",
        "schema_version": 2,
        "messages": [],
        "history": "bad",
    }
    before = copy.deepcopy(source)

    with pytest.raises(SessionMigrationError, match="history must be a list"):
        migrate_session_to_v3(source)

    assert source == before


def test_v2_resume_roundtrip_uses_messages_only(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    path = store.path_for("resume")
    path.write_text(json.dumps({
        "id": "resume",
        "schema_version": 2,
        "messages": [{"role": "user", "content": "q"}],
        "history": [{"role": "user", "content": "stale mirror"}],
    }), encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)

    first = Pico.from_session(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        session_id="resume",
        approval_policy="auto",
    )
    first_disk = json.loads(path.read_text(encoding="utf-8"))
    assert first_disk["schema_version"] == 3
    assert "history" not in first_disk
    assert "history" not in first.session

    second = Pico.from_session(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        session_id="resume",
        approval_policy="auto",
    )
    second_disk = json.loads(path.read_text(encoding="utf-8"))
    assert second_disk["schema_version"] == 3
    assert "history" not in second_disk
    assert "history" not in second.session


def test_v3_with_orphan_is_rejected():
    with pytest.raises(SessionMigrationError, match="orphan"):
        migrate_session_to_v3({
            "id": "s3",
            "schema_version": 3,
            "messages": [{
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": "x",
                    "name": "read_file",
                    "input": {},
                }],
                "_pico_meta": {},
            }],
        })
