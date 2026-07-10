import json
import os
import stat
from pathlib import Path
from contextlib import contextmanager

import pytest

import pico.session_store as session_store_module
from pico.messages import validate_messages
from pico.session_store import SessionStore


def test_session_store_saves_loads_and_finds_latest_session(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    first = {
        "id": "session_001",
        "schema_version": 2,
        "messages": [{"role": "user", "content": "first"}],
    }
    second = {
        "id": "session_002",
        "schema_version": 2,
        "messages": [{"role": "user", "content": "second"}],
    }

    first_path = store.save(first)
    second_path = store.save(second)

    assert first_path == store.path("session_001")
    assert json.loads(first_path.read_text(encoding="utf-8"))["id"] == "session_001"
    loaded = store.load("session_002")
    assert loaded["schema_version"] == 3
    assert "history" not in loaded
    assert loaded["messages"] == [
        {"role": "user", "content": "second", "_pico_meta": {}},
    ]
    validate_messages(loaded["messages"], require_meta=True)
    assert store.latest() == second_path.stem


def test_session_store_latest_is_none_when_empty(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")

    assert store.latest() is None


def test_session_store_saves_with_atomic_replace(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    replace_calls = []
    original_replace = Path.replace

    def tracking_replace(self, target):
        replace_calls.append((self.name, Path(target).name))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", tracking_replace)

    store.save({"id": "session_atomic", "history": []})

    assert replace_calls
    assert replace_calls[-1][1] == "session_atomic.json"
    assert not list((tmp_path / ".pico" / "sessions").glob("*.tmp"))


def test_session_store_save_uses_file_lock(tmp_path, monkeypatch):
    calls = []

    @contextmanager
    def fake_lock(path):
        calls.append(Path(path).name)
        yield

    monkeypatch.setattr(session_store_module.file_lock, "locked_file", fake_lock)

    store = SessionStore(tmp_path / ".pico" / "sessions")
    store.save({"id": "session_locked", "history": []})

    assert calls == [".session_store.lock"]


def test_session_store_uses_private_owner_only_paths(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")

    path = store.save({"id": "private", "history": []})

    if os.name == "posix":
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600


def test_session_store_load_refuses_symlink_file(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    outside = tmp_path / "outside.json"
    original = b'{"id":"linked","schema_version":3,"messages":[]}'
    outside.write_bytes(original)
    store.path("linked").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        store.load("linked")

    assert outside.read_bytes() == original
