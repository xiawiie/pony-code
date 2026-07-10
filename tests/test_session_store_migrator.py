import copy
import json

import pytest

from pico.messages import validate_messages
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
    assert loaded["schema_version"] == 2
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


def test_migrator_idempotent_on_v2(store, tmp_path):
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
    assert loaded["schema_version"] == 2
    # v2 不再触发 backup
    backup_dir = session_path.parent / "backup"
    assert not backup_dir.exists() or not list(backup_dir.glob("s2.v1.*.json"))


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
    match = re.match(r"s1\.v1\.(\d+)\.json$", backups[0].name)
    assert match is not None
    assert len(match.group(1)) >= 15, f"Expected nanosecond precision, got {match.group(1)!r}"


def test_migrator_preserves_created_at_and_tool_use_id():
    """Task E8: migrator must carry _pico_meta.created_at across every
    message type and set _pico_meta.tool_use_id on both halves of tool pairs."""
    from pico.session_store import _migrate_v1_to_v2

    v1 = {
        "id": "s",
        "schema_version": 1,
        "history": [
            {"role": "user", "content": "hi", "created_at": "2026-04-01T00:00:00Z"},
            {"role": "tool", "name": "read_file", "args": {"path": "x"}, "content": "y", "created_at": "2026-04-01T00:00:01Z"},
            {"role": "assistant", "content": "done", "created_at": "2026-04-01T00:00:02Z"},
        ],
    }
    v2 = _migrate_v1_to_v2(v1)
    msgs = v2["messages"]
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


def test_migrator_idempotent_returns_v2_verbatim():
    from pico.session_store import _migrate_v1_to_v2

    v2 = {
        "id": "s",
        "schema_version": 2,
        "messages": [
            {"role": "user", "content": "hi", "_pico_meta": {"created_at": "x"}},
        ],
    }
    result = _migrate_v1_to_v2(v2)
    # Idempotent — same messages list, unchanged.
    assert result["schema_version"] == 2
    assert result["messages"] == v2["messages"]


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


def test_empty_v1_history_migrates_to_empty_v3_messages():
    migrated = migrate_session_to_v3({
        "id": "empty",
        "schema_version": 1,
        "history": [],
    })
    assert migrated["schema_version"] == 3
    assert migrated["messages"] == []
    assert "history" not in migrated


def test_v3_is_validated_and_returned_without_history():
    source = {"id": "s3", "schema_version": 3, "messages": _valid_v2_messages()}
    assert migrate_session_to_v3(source) == source


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
