import json
import os
import stat
from pathlib import Path
from contextlib import contextmanager

import pytest

from pico import security as security_module
import pico.session_store as session_store_module
from pico.messages import validate_messages
from pico.session_store import SESSION_FORMAT_VERSION, SessionFormatError, SessionStore


def _session(session_id, content="hello"):
    return {
        "record_type": "session",
        "format_version": SESSION_FORMAT_VERSION,
        "id": session_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "workspace_root": "/repo",
        "messages": [{"role": "user", "content": content, "_pico_meta": {}}],
        "working_memory": {},
        "memory": {},
        "recently_recalled": [],
        "checkpoints": {},
        "resume_state": {},
        "recovery": {},
        "runtime_identity": {},
    }


def _provider_binding(**overrides):
    binding = {
        "protocol_family": "openai_responses",
        "model": "gpt-test",
        "endpoint_hash": "sha256:" + "a" * 64,
    }
    binding.update(overrides)
    return binding


def test_session_store_saves_loads_and_finds_latest_session(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    first = _session("session_001", "first")
    second = _session("session_002", "second")

    first_path = store.save(first)
    second_path = store.save(second)

    assert first_path == store.path("session_001")
    assert json.loads(first_path.read_text(encoding="utf-8"))["id"] == "session_001"
    loaded = store.load("session_002")
    assert loaded["record_type"] == "session"
    assert loaded["format_version"] == SESSION_FORMAT_VERSION == 2
    assert "history" not in loaded
    assert loaded["messages"] == [
        {"role": "user", "content": "second", "_pico_meta": {}},
    ]
    validate_messages(loaded["messages"], require_meta=True)
    os.utime(first_path, ns=(1, 1))
    os.utime(second_path, ns=(2, 2))
    assert store.latest() == second_path.stem


def test_session_store_round_trips_current_provider_binding(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    session = _session("provider-bound")
    session["provider_binding"] = _provider_binding()

    store.save(session)

    assert store.load("provider-bound")["provider_binding"] == _provider_binding()


@pytest.mark.parametrize(
    "binding",
    [
        _provider_binding(protocol_family="openai_chat"),
        {**_provider_binding(), "profile": "deepseek"},
        _provider_binding(endpoint_hash="sha256:" + "z" * 64),
        _provider_binding(endpoint_hash="a" * 64),
        {"protocol_family": "openai_responses"},
    ],
)
def test_session_store_rejects_invalid_provider_binding(tmp_path, binding):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    session = _session("provider-invalid")
    session["provider_binding"] = binding

    with pytest.raises(SessionFormatError, match="provider binding"):
        store.save(session)


def test_session_store_latest_is_none_when_empty(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")

    assert store.latest() is None


def test_session_store_saves_with_atomic_replace(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    replace_calls = []
    original_replace = security_module.os.replace

    def tracking_replace(source, target, **kwargs):
        replace_calls.append((Path(source).name, Path(target).name))
        return original_replace(source, target, **kwargs)

    monkeypatch.setattr(security_module.os, "replace", tracking_replace)

    store.save(_session("session_atomic"))

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
    store.save(_session("session_locked"))

    assert calls == [".session_store.lock"]


def test_session_store_parent_swap_cannot_redirect_record(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    original_root = tmp_path / "sessions-original"
    store.root.rename(original_root)
    store.root.mkdir()

    with pytest.raises(ValueError, match="private root changed"):
        store.save(_session("redirected"))

    assert not (store.root / "redirected.json").exists()
    assert not (original_root / "redirected.json").exists()


def test_session_store_uses_private_owner_only_paths(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")

    path = store.save(_session("private"))

    if os.name == "posix":
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600


def test_session_store_load_refuses_symlink_file(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    outside = tmp_path / "outside.json"
    original = b'{}'
    outside.write_bytes(original)
    store.lock_path.touch(mode=0o600)
    store.path("linked").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        store.load("linked")

    assert outside.read_bytes() == original


def test_session_store_load_rejects_oversized_record(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    monkeypatch.setattr(session_store_module, "MAX_SESSION_BYTES", 8)
    store.lock_path.touch(mode=0o600)
    path = store.path("oversized")
    path.write_bytes(b"x" * 9)

    with pytest.raises(ValueError, match="too large"):
        store.load("oversized")

    assert path.read_bytes() == b"x" * 9


def test_session_store_save_bounds_existing_record_before_backup(
    tmp_path,
    monkeypatch,
):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    session = _session("oversized_existing")
    rendered = (json.dumps(session, indent=2) + "\n").encode("utf-8")
    monkeypatch.setattr(session_store_module, "MAX_SESSION_BYTES", len(rendered))
    path = store.path(session["id"])
    original = b"x" * (len(rendered) + 1)
    path.write_bytes(original)

    with pytest.raises(ValueError, match="private file too large"):
        store.save(session)

    assert path.read_bytes() == original
    assert not list(store.root.glob(".*.bak"))


def test_session_store_save_advances_after_unlinked_backup_wipe_failure(
    tmp_path,
    monkeypatch,
):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    session = _session("cleanup_commit", "first")
    store.save(session)
    monkeypatch.setattr(
        security_module.os,
        "ftruncate",
        lambda _descriptor, _length: (_ for _ in ()).throw(
            OSError("open-unlinked wipe failed")
        ),
    )

    session["messages"].append(
        {"role": "user", "content": "second", "_pico_meta": {}}
    )
    assert store.save(session) == store.path(session["id"])
    session["messages"].append(
        {"role": "user", "content": "third", "_pico_meta": {}}
    )
    assert store.save(session) == store.path(session["id"])

    assert store.load(session["id"])["messages"] == session["messages"]
    assert not list(store.root.glob(".*.bak"))


def test_session_store_save_rejects_oversized_record_without_replacing_canonical(
    tmp_path,
    monkeypatch,
):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    session = _session("bounded", "ok")
    path = store.save(session)
    canonical = path.read_bytes()
    monkeypatch.setattr(session_store_module, "MAX_SESSION_BYTES", len(canonical))

    assert store.save(session) == path
    assert store.load("bounded") == session

    with pytest.raises(ValueError, match="private file too large"):
        store.save(_session("bounded", "x" * (len(canonical) + 1)))

    assert path.read_bytes() == canonical


@pytest.mark.parametrize("version", [None, True, 2.0, "2", 1])
def test_session_store_rejects_non_current_versions_without_rewrite(tmp_path, version):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    payload = _session("strict")
    if version is None:
        payload.pop("format_version")
    else:
        payload["format_version"] = version
    path = store.path("strict")
    store.lock_path.touch(mode=0o600)
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(SessionFormatError, match="format version|fields"):
        store.load("strict")

    assert path.read_bytes() == before


def test_session_store_rejects_nested_duplicate_keys(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    payload = json.dumps(_session("duplicate")).replace(
        '"runtime_identity": {}',
        '"runtime_identity": {"feature_flags": {}, "feature_flags": {}}',
    )
    store.lock_path.touch(mode=0o600)
    store.path("duplicate").write_text(payload, encoding="utf-8")

    with pytest.raises(SessionFormatError, match="duplicate"):
        store.load("duplicate")


@pytest.mark.parametrize("embedded", [False, True])
def test_session_store_rejects_unknown_feature_flag_identity(tmp_path, embedded):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    payload = _session("dead-flag")
    identity = {"feature_flags": {"prompt_cache": True}}
    if embedded:
        payload["checkpoints"] = {
            "items": {"ckpt": {"runtime_identity": identity}}
        }
    else:
        payload["runtime_identity"] = identity

    with pytest.raises(SessionFormatError, match="unsupported runtime identity feature flag"):
        store.save(payload)
