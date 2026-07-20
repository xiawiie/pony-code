from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import threading

import pytest

import pony.security.private_files as private_files_module
import pony.state.session_store as session_store_module
from pony.agent.messages import make_tool_pair, validate_messages
from pony.state.session_store import (
    LEGACY_SESSION_FORMAT_VERSION,
    MAX_SESSION_ENTRY_BYTES,
    PlanApprovalChanged,
    SESSION_FORMAT_VERSION,
    SessionFormatError,
    SessionMigrationRequired,
    SessionStore,
    SessionTailRepairRequired,
)


def _session(workspace, session_id, content="hello", *, legacy=False):
    session = {
        "record_type": "session",
        "format_version": (
            LEGACY_SESSION_FORMAT_VERSION if legacy else SESSION_FORMAT_VERSION
        ),
        "id": session_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "workspace_root": str(workspace),
        "messages": [{"role": "user", "content": content, "_pony_meta": {}}],
        "working_memory": {"task_summary": "", "recent_files": []},
        "memory": {"file_summaries": {}},
        "recently_recalled": [],
        "checkpoints": {},
        "resume_state": {},
        "runtime_identity": {},
    }
    if not legacy:
        session.update(
            permission_mode="auto",
            permission_rules={"allow": [], "ask": [], "deny": []},
            plan_text="",
            plan_revision=0,
            pre_plan_mode="",
        )
    return session


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _provider_binding(**overrides):
    binding = {
        "protocol_family": "openai_responses",
        "model": "gpt-test",
        "endpoint_hash": "sha256:" + "a" * 64,
    }
    binding.update(overrides)
    return binding


def test_legacy_readonly_inspection_allows_owner_parent_mode_0755(tmp_path):
    root = tmp_path / "sessions"
    store = SessionStore(root)
    store.save(_session(tmp_path, "seed"))
    legacy = store.legacy_path("legacy-readonly")
    legacy.write_text(
        json.dumps(_session(tmp_path, "legacy-readonly", legacy=True)),
        encoding="utf-8",
    )
    legacy.chmod(0o600)
    root.chmod(0o755)

    storage, payload, tree = store.inspect_readonly("legacy-readonly")

    assert storage == "legacy"
    assert payload["id"] == "legacy-readonly"
    assert tree is None
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_session_store_saves_loads_and_finds_latest_session(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    first = _session(tmp_path, "session_001", "first")
    second = _session(tmp_path, "session_002", "second")

    first_path = store.save(first)
    second_path = store.save(second)

    assert first_path == store.path("session_001")
    assert first_path.suffix == ".jsonl"
    assert _jsonl(first_path)[0]["record_type"] == "session_header"
    loaded = store.load("session_002")
    assert loaded["format_version"] == SESSION_FORMAT_VERSION
    assert loaded["record_type"] == "session"
    assert loaded["format_version"] == SESSION_FORMAT_VERSION == 5
    assert "history" not in loaded
    assert loaded["messages"] == [
        {"role": "user", "content": "second", "_pony_meta": {}},
    ]
    validate_messages(loaded["messages"], require_meta=True)
    os.utime(first_path, ns=(1, 1))
    os.utime(second_path, ns=(2, 2))
    assert store.latest() == "session_002"


def test_new_session_is_header_plus_append_only_entries(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "tree")
    path = store.save(session)
    original = path.read_bytes()

    session["messages"].append(
        {"role": "assistant", "content": "done", "_pony_meta": {}}
    )
    store.save(session)

    current = path.read_bytes()
    assert current.startswith(original)
    rows = _jsonl(path)
    assert [row["type"] for row in rows[1:]] == [
        "session_info",
        "permission_mode_change",
        "message",
        "message",
    ]
    assert {"working_memory", "memory", "recently_recalled"}.isdisjoint(
        rows[1]["data"]["set"]
    )
    assert store.load("tree") == session


def test_tool_call_and_result_are_one_atomic_entry(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "tools")
    store.save(session)
    assistant, result = make_tool_pair(
        name="read_file",
        arguments={"path": "a.py"},
        tool_use_id="tool-1",
        result_content="ok",
        created_at="2026-01-01T00:00:01+00:00",
        tool_status="ok",
        effect_class="workspace_read",
    )
    session["messages"].extend((assistant, result))

    store.save(session)

    entries = store.entries("tools")
    exchanges = [entry for entry in entries if entry["type"] == "tool_exchange"]
    assert len(exchanges) == 1
    assert exchanges[0]["data"] == {"assistant": assistant, "result": result}
    assert store.load("tools")["messages"] == session["messages"]


def test_fork_selects_parent_path_without_deleting_old_branch(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "branch", "one")
    session["messages"].append(
        {"role": "assistant", "content": "two", "_pony_meta": {}}
    )
    store.save(session)
    before = store.entries("branch")
    first_message = next(entry for entry in before if entry["type"] == "message")

    fork_entry = store.fork("branch", first_message["id"])
    tree = store.load_tree("branch")

    assert fork_entry["parent_id"] == first_message["id"]
    assert len(tree.entries) == len(before) + 1
    assert tree.projection["messages"] == [session["messages"][0]]
    assert any(entry["id"] == before[-1]["id"] for entry in tree.entries)


def test_non_prefix_save_creates_new_branch_and_preserves_history(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "rewrite", "old")
    store.save(session)
    old_ids = {entry["id"] for entry in store.entries("rewrite")}
    session["messages"] = [
        {"role": "user", "content": "new", "_pony_meta": {}}
    ]

    store.save(session)

    tree = store.load_tree("rewrite")
    assert tree.projection == session
    assert old_ids < {entry["id"] for entry in tree.entries}
    assert any(entry["type"] == "rewind" for entry in tree.entries)


def test_session_store_round_trips_current_provider_binding(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "provider-bound")
    session["provider_binding"] = _provider_binding()

    store.save(session)

    assert store.load("provider-bound")["provider_binding"] == _provider_binding()


def test_session_store_rejects_provider_binding_change(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "provider-bound")
    session["provider_binding"] = _provider_binding()
    store.save(session)

    session["provider_binding"] = _provider_binding(model="another-model")

    with pytest.raises(SessionFormatError, match="provider binding changed"):
        store.save(session)


def test_session_store_set_provider_model_uses_expected_binding_cas(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "provider-model-cas")
    original = _provider_binding()
    changed = _provider_binding(model="gpt-next")
    session["provider_binding"] = original
    store.save(session)

    entry = store.set_provider_model(
        session["id"],
        changed,
        expected_binding=original,
        expected_leaf_id=store.load_tree(session["id"]).leaf_id,
    )

    assert entry["data"]["set"]["provider_binding"] == changed
    assert store.load(session["id"])["provider_binding"] == changed
    before = store.path(session["id"]).read_bytes()
    with pytest.raises(SessionFormatError, match="^model_session_mismatch$"):
        store.set_provider_model(
            session["id"],
            _provider_binding(model="gpt-later"),
            expected_binding=original,
            expected_leaf_id=store.load_tree(session["id"]).leaf_id,
        )
    assert store.path(session["id"]).read_bytes() == before


@pytest.mark.parametrize(
    "candidate",
    [
        _provider_binding(model="gpt-next", protocol_family="openai_chat_completions"),
        _provider_binding(model="gpt-next", endpoint_hash="sha256:" + "b" * 64),
    ],
)
def test_session_store_set_provider_model_rejects_target_drift_without_writing(
    tmp_path,
    candidate,
):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "provider-model-drift")
    original = _provider_binding()
    session["provider_binding"] = original
    store.save(session)
    before = store.path(session["id"]).read_bytes()

    with pytest.raises(SessionFormatError, match="^model_session_mismatch$"):
        store.set_provider_model(
            session["id"],
            candidate,
            expected_binding=original,
            expected_leaf_id=store.load_tree(session["id"]).leaf_id,
        )

    assert store.path(session["id"]).read_bytes() == before


def test_append_messages_rejects_changed_expected_leaf(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "message-leaf-cas")
    session["provider_binding"] = _provider_binding()
    store.save(session)
    original = store.load_tree(session["id"])
    store.label(session["id"], "concurrent")
    before = store.path(session["id"]).read_bytes()

    with pytest.raises(
        SessionFormatError,
        match="^session changed before message append$",
    ):
        store.append_messages(
            session["id"],
            [{"role": "assistant", "content": "stale", "_pony_meta": {}}],
            expected_leaf_id=original.leaf_id,
            expected_provider_binding=original.projection["provider_binding"],
        )

    assert store.path(session["id"]).read_bytes() == before


def test_append_messages_rejects_changed_expected_provider_binding(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "message-binding-cas")
    original = _provider_binding()
    session["provider_binding"] = original
    store.save(session)
    tree = store.load_tree(session["id"])
    store.set_provider_model(
        session["id"],
        _provider_binding(model="gpt-next"),
        expected_binding=original,
        expected_leaf_id=tree.leaf_id,
    )
    before = store.path(session["id"]).read_bytes()

    with pytest.raises(SessionFormatError, match="^model_session_mismatch$"):
        store.append_messages(
            session["id"],
            [{"role": "assistant", "content": "stale", "_pony_meta": {}}],
            expected_leaf_id=tree.leaf_id,
            expected_provider_binding=original,
        )

    assert store.path(session["id"]).read_bytes() == before


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
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "provider-invalid")
    session["provider_binding"] = binding

    with pytest.raises(SessionFormatError, match="provider binding"):
        store.save(session)


def test_session_store_latest_is_none_when_empty(tmp_path):
    assert SessionStore(tmp_path / ".pony" / "sessions").latest() is None


def test_session_store_save_uses_file_lock(tmp_path, monkeypatch):
    calls = []

    @contextmanager
    def fake_lock(path, **_kwargs):
        calls.append(Path(path).name)
        yield

    monkeypatch.setattr(session_store_module.file_lock, "locked_file", fake_lock)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    store.save(_session(tmp_path, "session_locked"))
    assert calls == [".session_store.lock"]


def test_session_store_parent_swap_cannot_redirect_record(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    original_root = tmp_path / "sessions-original"
    store.root.rename(original_root)
    store.root.mkdir()

    with pytest.raises(ValueError, match="private root changed"):
        store.save(_session(tmp_path, "redirected"))

    assert not (store.root / "redirected.jsonl").exists()
    assert not (original_root / "redirected.jsonl").exists()


def test_session_store_uses_private_owner_only_paths(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = store.save(_session(tmp_path, "private"))
    if os.name == "posix":
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600


def test_session_store_load_refuses_symlink_file(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"{}\n")
    store.lock_path.touch(mode=0o600)
    store.path("linked").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        store.load("linked")
    assert outside.read_bytes() == b"{}\n"


def test_session_store_load_rejects_oversized_record(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    monkeypatch.setattr(session_store_module, "MAX_SESSION_BYTES", 8)
    store.lock_path.touch(mode=0o600)
    path = store.path("oversized")
    path.write_bytes(b"x" * 9)
    with pytest.raises(ValueError, match="too large"):
        store.load("oversized")
    assert path.read_bytes() == b"x" * 9


def test_incomplete_tail_requires_explicit_repair(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "tail")
    path = store.save(session)
    original = path.read_bytes()
    path.write_bytes(original + b'{"record_type":"session_entry"')

    with pytest.raises(SessionTailRepairRequired):
        store.load("tail")
    assert path.read_bytes() != original

    assert store.repair_tail("tail") is True
    assert path.read_bytes() == original
    assert store.load("tail") == session


def test_session_entry_hard_cap_is_enforced_without_partial_append(
    tmp_path,
    monkeypatch,
):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "bounded")
    path = store.save(session)
    original = path.read_bytes()
    monkeypatch.setattr(session_store_module, "MAX_SESSION_ENTRY_BYTES", 256)
    session["messages"].append(
        {"role": "assistant", "content": "x" * 512, "_pony_meta": {}}
    )
    with pytest.raises(ValueError, match="entry too large"):
        store.save(session)
    assert path.read_bytes() == original


def test_legacy_json_migrates_only_on_explicit_resume_with_backup(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    legacy = _session(tmp_path, "legacy", legacy=True)
    store.lock_path.touch(mode=0o600)
    store.legacy_path("legacy").write_text(
        json.dumps(legacy),
        encoding="utf-8",
    )

    with pytest.raises(SessionMigrationRequired):
        store.load("legacy")
    loaded = store.load_for_resume("legacy")

    assert loaded == {
        **legacy,
        "format_version": SESSION_FORMAT_VERSION,
        "permission_mode": "default",
        "permission_rules": {"allow": [], "ask": [], "deny": []},
        "plan_text": "",
        "plan_revision": 0,
        "pre_plan_mode": "",
    }
    assert store.path("legacy").exists()
    assert not store.legacy_path("legacy").exists()
    backups = list((store.root / "legacy-backups").glob("legacy.*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == legacy
    assert sum(entry["type"] == "migration" for entry in store.entries("legacy")) == 1
    assert store.load("legacy") == loaded


def test_legacy_migration_promotes_working_state_to_task_checkpoint(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    legacy = _session(tmp_path, "legacy-working", legacy=True)
    legacy["working_memory"] = {
        "task_summary": "finish migration",
        "recent_files": ["src/current.py"],
    }
    legacy["memory"] = {
        "file_summaries": {
            "src/current.py": "current module",
            "src/stale.py": "must not migrate",
        }
    }
    store.lock_path.touch(mode=0o600)
    store.legacy_path("legacy-working").write_text(
        json.dumps(legacy),
        encoding="utf-8",
    )

    loaded = store.load_for_resume("legacy-working")

    checkpoint_id = loaded["checkpoints"]["current_id"]
    assert checkpoint_id.startswith("ckpt_migrated_")
    checkpoint = loaded["checkpoints"]["items"][checkpoint_id]
    assert checkpoint["goal"] == "finish migration"
    assert checkpoint["key_files"] == [
        {
            "path": "src/current.py",
            "freshness": {},
            "summary": "current module",
        }
    ]
    assert "workspace_checkpoint_id" not in checkpoint
    assert loaded["working_memory"] == {
        "task_summary": "finish migration",
        "recent_files": ["src/current.py"],
    }
    assert loaded["memory"] == {
        "file_summaries": {"src/current.py": "current module"}
    }
    assert any(
        entry["type"] == "task_checkpoint"
        and entry["data"]["checkpoint_id"] == checkpoint_id
        for entry in store.entries("legacy-working")
    )


def test_legacy_migration_promotes_checkpoint_without_overwriting_current_state(
    tmp_path,
):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    legacy = _session(tmp_path, "legacy-checkpoint", legacy=True)
    legacy["runtime_identity"] = {
        "model": "current-model",
        "feature_flags": {},
    }
    legacy["checkpoints"] = {
        "current_id": "checkpoint-old",
        "items": {
            "checkpoint-old": {
                "checkpoint_id": "checkpoint-old",
                "created_at": "2026-01-01T00:00:00+00:00",
                "runtime_identity": {
                    "model": "old-model",
                    "feature_flags": {},
                },
            }
        },
    }
    store.lock_path.touch(mode=0o600)
    store.legacy_path("legacy-checkpoint").write_text(
        json.dumps(legacy),
        encoding="utf-8",
    )

    loaded = store.load_for_resume("legacy-checkpoint")

    expected = {
        **legacy,
        "format_version": SESSION_FORMAT_VERSION,
        "permission_mode": "default",
        "permission_rules": {"allow": [], "ask": [], "deny": []},
        "plan_text": "",
        "plan_revision": 0,
        "pre_plan_mode": "",
    }
    assert loaded == expected
    assert any(
        entry["type"] == "task_checkpoint"
        for entry in store.entries("legacy-checkpoint")
    )
    assert loaded["runtime_identity"] == legacy["runtime_identity"]
    assert "recovery" not in loaded


def test_failed_legacy_publish_keeps_old_session_and_is_retryable(
    tmp_path,
    monkeypatch,
):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    legacy = _session(tmp_path, "retry", legacy=True)
    store.lock_path.touch(mode=0o600)
    store.legacy_path("retry").write_text(json.dumps(legacy), encoding="utf-8")
    real_replace = session_store_module.os.replace

    def fail_replace(*_args, **_kwargs):
        raise OSError("candidate publish crash")

    monkeypatch.setattr(session_store_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publish crash"):
        store.load_for_resume("retry")
    assert store.legacy_path("retry").exists()
    assert not store.path("retry").exists()

    monkeypatch.setattr(session_store_module.os, "replace", real_replace)
    assert store.load_for_resume("retry")["messages"] == legacy["messages"]


def test_invalid_legacy_session_is_never_rewritten(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    store.lock_path.touch(mode=0o600)
    path = store.legacy_path("invalid")
    path.write_text('{"record_type":"session","format_version":1}', encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(SessionFormatError):
        store.load_for_resume("invalid")
    assert path.read_bytes() == original
    assert not store.path("invalid").exists()


def test_legacy_nested_duplicate_keys_are_rejected(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    payload = json.dumps(_session(tmp_path, "duplicate", legacy=True)).replace(
        '"runtime_identity": {}',
        '"runtime_identity": {"feature_flags": {}, "feature_flags": {}}',
    )
    store.lock_path.touch(mode=0o600)
    store.legacy_path("duplicate").write_text(payload, encoding="utf-8")
    with pytest.raises(SessionFormatError, match="duplicate"):
        store.load_for_resume("duplicate")


@pytest.mark.parametrize("embedded", [False, True])
def test_session_store_rejects_unknown_feature_flag_identity(
    tmp_path,
    embedded,
):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    payload = _session(tmp_path, "dead-flag")
    identity = {"feature_flags": {"prompt_cache": True}}
    if embedded:
        payload["checkpoints"] = {
            "current_id": "ckpt",
            "items": {"ckpt": {"runtime_identity": identity}},
        }
    else:
        payload["runtime_identity"] = identity
    with pytest.raises(
        SessionFormatError,
        match="unsupported runtime identity feature flag",
    ):
        store.save(payload)


def test_worktree_identity_tamper_is_rejected(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = store.save(_session(tmp_path, "identity"))
    rows = _jsonl(path)
    rows[0]["worktree_identity"]["root_inode"] += 1
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    with pytest.raises(SessionFormatError, match="identity digest mismatch"):
        store.load("identity")


def test_clone_to_worktree_copies_active_branch_and_clears_workspace_state(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    store = SessionStore(source / ".pony" / "sessions")
    session = _session(source, "source-session")
    session["messages"].append(
        {"role": "assistant", "content": "done", "_pony_meta": {}}
    )
    session["working_memory"] = {
        "task_summary": "continue",
        "recent_files": ["old.py"],
    }
    session["memory"] = {"file_summaries": {"old.py": "stale"}}
    session["checkpoints"] = {
        "current_id": "checkpoint-old",
        "items": {
            "checkpoint-old": {
                "checkpoint_id": "checkpoint-old",
                "created_at": "2026-01-01T00:00:00+00:00",
                "goal": "continue",
                "status": "in_progress",
            }
        },
    }
    store.save(session)
    store.set_permission_mode("source-session", "plan")
    first_message = next(
        entry for entry in store.entries("source-session") if entry["type"] == "message"
    )
    store.append_control(
        "source-session",
        "compaction",
        {
            "summary": "source summary",
            "first_kept_entry_id": first_message["id"],
            "tokens_before": 100,
            "summary_tokens": 5,
            "tail_tokens": 10,
            "reason": "test",
        },
    )
    published = target / ".pony" / "sessions" / "target-session.jsonl"
    original_atomic_write = session_store_module.write_private_bytes_atomic

    def assert_complete_before_publish(path, data, **kwargs):
        if path == published:
            assert not published.exists()
            session_store_module._parse_jsonl(data, "target-session")
            assert kwargs["require_absent"] is True
        return original_atomic_write(path, data, **kwargs)

    monkeypatch.setattr(
        session_store_module,
        "write_private_bytes_atomic",
        assert_complete_before_publish,
    )

    cloned = store.clone_to_worktree(
        "source-session",
        target,
        new_session_id="target-session",
    )
    target_store = SessionStore(target / ".pony" / "sessions")
    loaded = target_store.load("target-session")
    view = target_store.context_view("target-session")

    assert cloned["session_id"] == "target-session"
    assert loaded["workspace_root"] == str(target)
    assert loaded["messages"] == session["messages"]
    assert loaded["working_memory"] == {
        "task_summary": "continue",
        "recent_files": [],
    }
    assert loaded["memory"] == {"file_summaries": {}}
    cloned_checkpoint_id = loaded["checkpoints"]["current_id"]
    assert cloned_checkpoint_id.startswith("checkpoint-old-clone-")
    cloned_checkpoint = loaded["checkpoints"]["items"][cloned_checkpoint_id]
    assert cloned_checkpoint["goal"] == "continue"
    assert "workspace_checkpoint_id" not in cloned_checkpoint
    assert cloned_checkpoint["context_usage"] == {}
    assert cloned_checkpoint["key_files"] == []
    assert cloned_checkpoint["read_files"] == []
    assert cloned_checkpoint["modified_files"] == []
    assert cloned_checkpoint["worktree_identity_digest"] == (
        target_store.load_tree("target-session").header["worktree_identity"][
            "digest"
        ]
    )
    assert "recovery" not in loaded
    assert loaded["runtime_identity"] == {}
    assert loaded["permission_mode"] == "plan"
    assert view.summary == "source summary"
    assert target_store.load_tree("target-session").header["worktree_identity"][
        "lexical_root"
    ] == str(target)
    assert not any(
        path.name.startswith(".clone-")
        for path in (target / ".pony" / "sessions").iterdir()
    )


def test_rewind_expected_leaf_rejects_concurrent_session_change(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    store.save(_session(tmp_path, "rewind-cas"))
    tree = store.load_tree("rewind-cas")
    target = next(entry for entry in tree.active_path if entry["type"] == "message")
    store.label("rewind-cas", "concurrent")
    before = store.path("rewind-cas").read_bytes()

    with pytest.raises(SessionFormatError, match="session changed before control append"):
        store.rewind(
            "rewind-cas",
            target["id"],
            expected_leaf_id=tree.leaf_id,
        )

    assert store.path("rewind-cas").read_bytes() == before


def test_clone_publish_does_not_overwrite_concurrently_created_session(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    store = SessionStore(source / ".pony" / "sessions")
    store.save(_session(source, "source-session"))
    published = target / ".pony" / "sessions" / "target-session.jsonl"
    original_install = private_files_module._install_private_temp

    def create_target_after_final_check(state):
        if state.path == published:
            published.write_bytes(b"concurrent session\n")
        return original_install(state)

    monkeypatch.setattr(
        private_files_module,
        "_install_private_temp",
        create_target_after_final_check,
    )

    with pytest.raises(ValueError, match="clone session id already exists"):
        store.clone_to_worktree(
            "source-session",
            target,
            new_session_id="target-session",
        )

    assert published.read_bytes() == b"concurrent session\n"


def test_clone_publish_rolls_back_when_worktree_identity_drifts(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    store = SessionStore(source / ".pony" / "sessions")
    store.save(_session(source, "source-session"))
    published = target / ".pony" / "sessions" / "target-session.jsonl"
    original_install = private_files_module._install_private_temp

    def drift_after_install(state):
        original_install(state)
        if state.path == published:
            (target / ".git").mkdir()

    monkeypatch.setattr(
        private_files_module,
        "_install_private_temp",
        drift_after_install,
    )

    with pytest.raises(SessionFormatError, match="clone target worktree changed"):
        store.clone_to_worktree(
            "source-session",
            target,
            new_session_id="target-session",
        )

    assert not published.exists()


def test_clone_publish_rolls_back_when_legacy_session_appears(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    store = SessionStore(source / ".pony" / "sessions")
    store.save(_session(source, "source-session"))
    target_store = SessionStore(target / ".pony" / "sessions")
    published = target_store.path("target-session")
    legacy = target_store.legacy_path("target-session")
    original_install = private_files_module._install_private_temp

    def install_with_legacy_race(state):
        original_install(state)
        if state.path == published:
            legacy.write_bytes(b"legacy session\n")

    monkeypatch.setattr(
        private_files_module,
        "_install_private_temp",
        install_with_legacy_race,
    )

    with pytest.raises(ValueError, match="clone session id already exists"):
        store.clone_to_worktree(
            "source-session",
            target,
            new_session_id="target-session",
        )

    assert not published.exists()
    assert legacy.read_bytes() == b"legacy session\n"


def test_session_tree_source_has_expected_line_limit_constant():
    assert MAX_SESSION_ENTRY_BYTES == 8 * 1024 * 1024


def test_plan_state_requires_text_and_positive_revision_together(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "invalid-plan-state")
    session["plan_text"] = "# Plan"

    with pytest.raises(SessionFormatError, match="invalid plan state"):
        store.save(session)


def test_generic_control_writer_cannot_append_plan_artifact(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    store.save(_session(tmp_path, "plan-writer"))

    with pytest.raises(ValueError, match="invalid control entry type"):
        store.append_control(
            "plan-writer",
            "plan_artifact",
            {"text": "# Plan", "revision": 1},
        )

    assert store.load("plan-writer")["plan_revision"] == 0


def test_force_branch_projection_mismatch_is_zero_write(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = store.save(_session(tmp_path, "branch-projection"))
    candidate = store.load("branch-projection")
    candidate["permission_mode"] = "plan"
    original = path.read_bytes()

    with pytest.raises(SessionFormatError, match="session tree projection mismatch"):
        store.save(candidate, force_branch=True)

    assert path.read_bytes() == original
    assert store.load("branch-projection")["permission_mode"] == "auto"


def test_concurrent_permission_rule_writers_preserve_all_updates(tmp_path):
    root = tmp_path / ".pony" / "sessions"
    seed = SessionStore(root)
    seed.save(_session(tmp_path, "permission-race"))
    updates = (
        ("read_file", "deny"),
        ("write_file", "allow"),
        ("patch_file", "ask"),
        ("run_shell", "deny"),
        ("search", "allow"),
        ("memory_save", "ask"),
    )
    barrier = threading.Barrier(len(updates))
    errors = []

    def write_rule(name, behavior):
        try:
            store = SessionStore(root)
            barrier.wait(timeout=5)
            store.set_permission_rule("permission-race", name, behavior)
        except Exception as exc:  # noqa: BLE001 - collect thread failures
            errors.append(exc)

    threads = [threading.Thread(target=write_rule, args=update) for update in updates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    rules = seed.load("permission-race")["permission_rules"]
    assert rules == {
        "allow": ["search", "write_file"],
        "ask": ["memory_save", "patch_file"],
        "deny": ["read_file", "run_shell"],
    }


def test_concurrent_plan_writers_allocate_distinct_revisions(tmp_path):
    root = tmp_path / ".pony" / "sessions"
    seed = SessionStore(root)
    seed.save(_session(tmp_path, "plan-race"))
    count = 8
    barrier = threading.Barrier(count)
    errors = []

    def write_plan(index):
        try:
            store = SessionStore(root)
            barrier.wait(timeout=5)
            store.set_plan_text("plan-race", f"# Plan {index}")
        except Exception as exc:  # noqa: BLE001 - collect thread failures
            errors.append(exc)

    threads = [threading.Thread(target=write_plan, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert not any(thread.is_alive() for thread in threads)
    revisions = [
        entry["data"]["revision"]
        for entry in seed.load_tree("plan-race").entries
        if entry["type"] == "plan_artifact"
    ]
    assert sorted(revisions) == list(range(1, count + 1))


@pytest.mark.parametrize(
    "changed_expectation",
    (
        {"expected_plan_text": "# Other"},
        {"expected_revision": 0},
        {"expected_permission_mode": "auto"},
    ),
)
def test_plan_editor_save_requires_the_exact_opened_state(
    tmp_path,
    changed_expectation,
):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    store.save(_session(tmp_path, "plan-editor-state"))
    store.update_permissions("plan-editor-state", mode="plan")
    store.set_plan_text("plan-editor-state", "# Original")
    tree = store.load_tree("plan-editor-state")
    expected = {
        "expected_leaf_id": tree.leaf_id,
        "expected_plan_text": "# Original",
        "expected_revision": 1,
        "expected_permission_mode": "plan",
        **changed_expectation,
    }

    with pytest.raises(PlanApprovalChanged, match="plan changed while editing"):
        store.set_plan_text("plan-editor-state", "# Edited", **expected)

    assert store.load_tree("plan-editor-state").leaf_id == tree.leaf_id


def test_fork_expected_leaf_rejects_concurrent_session_change(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    store.save(_session(tmp_path, "fork-cas"))
    tree = store.load_tree("fork-cas")
    target = next(entry for entry in tree.active_path if entry["type"] == "message")
    store.label("fork-cas", "concurrent")
    before = store.path("fork-cas").read_bytes()

    with pytest.raises(SessionFormatError, match="session changed before control append"):
        store.fork(
            "fork-cas",
            target["id"],
            expected_leaf_id=tree.leaf_id,
        )

    assert store.path("fork-cas").read_bytes() == before


def test_permission_rule_batch_uses_one_atomic_session_entry(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    store.save(_session(tmp_path, "permission-batch"))
    before = len(store.load_tree("permission-batch").entries)

    store.set_permission_rules(
        "permission-batch",
        (
            ("write_file", "allow"),
            ("run_shell", "deny"),
            ("write_file", "deny"),
        ),
    )

    tree = store.load_tree("permission-batch")
    assert len(tree.entries) == before + 1
    assert tree.projection["permission_rules"] == {
        "allow": [],
        "ask": [],
        "deny": ["run_shell", "write_file"],
    }


def test_permission_mode_and_rule_batch_share_one_atomic_append(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    store.save(_session(tmp_path, "permission-transaction"))
    before = len(store.load_tree("permission-transaction").entries)

    result = store.update_permissions(
        "permission-transaction",
        mode="manual",
        rule_updates=(("write_file", "deny"),),
    )

    tree = store.load_tree("permission-transaction")
    assert len(tree.entries) == before + 2
    assert tree.entries[-1]["parent_id"] == result["mode_entry"]["id"]
    assert tree.projection["permission_mode"] == "default"
    assert tree.projection["permission_rules"]["deny"] == ["write_file"]


def test_save_checkpoint_preserves_current_runtime_state(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    session = _session(tmp_path, "checkpoint-state")
    session["runtime_identity"] = {
        "model": "current-model",
        "feature_flags": {},
    }
    store.save(session)

    session["checkpoints"] = {
        "current_id": "checkpoint-old",
        "items": {
            "checkpoint-old": {
                "checkpoint_id": "checkpoint-old",
                "created_at": "2026-01-01T00:00:00+00:00",
                "runtime_identity": {
                    "model": "old-model",
                    "feature_flags": {},
                },
            }
        },
    }
    store.save(session)

    loaded = store.load("checkpoint-state")
    assert loaded["checkpoints"] == session["checkpoints"]
    assert loaded["runtime_identity"] == session["runtime_identity"]
    assert "recovery" not in loaded
    assert [entry["type"] for entry in store.entries("checkpoint-state")][-2:] == [
        "task_checkpoint",
        "session_info",
    ]
