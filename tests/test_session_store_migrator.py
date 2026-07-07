import json
from pathlib import Path

import pytest

from pico.session_store import SessionStore


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
