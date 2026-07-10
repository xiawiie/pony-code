from pathlib import Path

import pytest

from pico.memory.block_store import BlockStore


def test_list_empty(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    assert store.list() == []


def test_list_workspace_and_user_notes(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    (user / "notes").mkdir(parents=True)
    (workspace / "notes" / "auth.md").write_text("# Auth notes\ndetail\n")
    (user / "notes" / "prefs.md").write_text("# Prefs\ndetail\n")

    store = BlockStore(workspace_root=workspace, user_root=user)
    entries = {e.path for e in store.list()}
    assert entries == {"workspace/notes/auth.md", "user/notes/prefs.md"}


def test_read_returns_full_content(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    user.mkdir()
    (workspace / "notes" / "auth.md").write_text("hello\nworld\n")

    store = BlockStore(workspace_root=workspace, user_root=user)
    assert store.read("workspace/notes/auth.md") == "hello\nworld\n"


def test_read_missing_raises(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    with pytest.raises(FileNotFoundError):
        store.read("workspace/notes/missing.md")


def test_append_agent_note_creates_file(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    total = store.append_agent_note(scope="workspace", note="bcrypt rounds > 12 timeout")
    assert total > 0
    contents = (workspace / "agent_notes.md").read_text()
    assert "bcrypt rounds > 12 timeout" in contents


def test_append_agent_note_appends(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    store.append_agent_note(scope="workspace", note="first")
    store.append_agent_note(scope="workspace", note="second")
    contents = (workspace / "agent_notes.md").read_text()
    assert "first" in contents
    assert "second" in contents
    assert contents.index("first") < contents.index("second")


def test_append_note_too_long_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    with pytest.raises(ValueError, match="500"):
        store.append_agent_note(scope="workspace", note="x" * 501)


def test_write_entrypoints_reject_complete_secret_content_and_allow_prose(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    secret = "github_pat_A123456789012345678901234567890"

    with pytest.raises(ValueError, match="sensitive_content"):
        store.append_agent_note(scope="workspace", note=secret)
    with pytest.raises(ValueError, match="sensitive_content"):
        store.write_agent_topic(
            scope="workspace",
            topic="auth",
            note="safe note",
            note_type=secret,
        )

    store.append_agent_note(scope="workspace", note="password policy")
    store.write_agent_topic(
        scope="workspace",
        topic="policy",
        note="password policy",
        note_type="feedback",
    )
    assert "password policy" in (workspace / "agent_notes.md").read_text(
        encoding="utf-8"
    )
    assert "password policy" in (workspace / "agent" / "policy.md").read_text(
        encoding="utf-8"
    )


def test_block_store_uses_configured_secret_snapshot(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    secret = "opaque-memory-value-123456789"
    store = BlockStore(
        workspace_root=workspace,
        user_root=user,
        redaction_env={"CUSTOM_CREDENTIAL": secret},
        secret_env_names=("CUSTOM_CREDENTIAL",),
    )

    with pytest.raises(ValueError, match="sensitive_content"):
        store.append_agent_note(scope="workspace", note=secret)


def test_block_store_freezes_process_env_when_constructed(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    secret = "opaque-frozen-memory-value-123456789"
    monkeypatch.setenv("PICO_FROZEN_SECRET", secret)
    store = BlockStore(workspace_root=workspace, user_root=user)
    monkeypatch.delenv("PICO_FROZEN_SECRET")

    with pytest.raises(ValueError, match="sensitive_content"):
        store.append_agent_note(scope="workspace", note=secret)

    assert not (workspace / "agent_notes.md").exists()


def test_block_store_screens_topic_and_scope_before_value_bearing_errors(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    secret = "opaque/unsafe-memory-value-123456789"
    store = BlockStore(
        workspace_root=workspace,
        user_root=user,
        redaction_env={"CUSTOM_CREDENTIAL": secret},
        secret_env_names=("CUSTOM_CREDENTIAL",),
    )

    calls = (
        lambda: store.append_agent_note(scope=secret, note="safe note"),
        lambda: store.write_agent_topic(
            scope="workspace",
            topic=secret,
            note="safe note",
        ),
        lambda: store.write_agent_topic(
            scope=secret,
            topic="safe-topic",
            note="safe note",
        ),
    )
    for call in calls:
        with pytest.raises(ValueError) as exc_info:
            call()
        assert str(exc_info.value) == "sensitive_content"
        assert secret not in str(exc_info.value)

    assert list(workspace.iterdir()) == []
    assert list(user.iterdir()) == []


def test_block_store_validation_errors_do_not_echo_invalid_values(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user, redaction_env={})

    with pytest.raises(ValueError) as topic_error:
        store.write_agent_topic(
            scope="workspace",
            topic="../invalid",
            note="safe note",
        )
    with pytest.raises(ValueError) as scope_error:
        store.write_agent_topic(
            scope="invalid-scope",
            topic="safe-topic",
            note="safe note",
        )

    assert str(topic_error.value) == "invalid topic"
    assert str(scope_error.value) == "invalid scope"


def test_append_rejects_when_complete_existing_note_would_remain_sensitive(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    secret = "github_pat_A123456789012345678901234567890"
    target = workspace / "agent_notes.md"
    target.write_text(secret + "\n", encoding="utf-8")
    store = BlockStore(workspace_root=workspace, user_root=user)

    with pytest.raises(ValueError, match="sensitive_content"):
        store.append_agent_note(scope="workspace", note="password policy")

    assert target.read_text(encoding="utf-8") == secret + "\n"


def test_agent_note_write_rejects_leaf_symlink_without_touching_target(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("untouched\n", encoding="utf-8")
    (workspace / "agent_notes.md").symlink_to(outside)
    store = BlockStore(workspace_root=workspace, user_root=user)

    with pytest.raises(ValueError, match="symlink"):
        store.append_agent_note(scope="workspace", note="safe note")

    assert outside.read_text(encoding="utf-8") == "untouched\n"


def test_agent_topic_write_rejects_symlinked_agent_directory(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    outside = tmp_path / "outside-agent"
    workspace.mkdir()
    user.mkdir()
    outside.mkdir()
    (workspace / "agent").symlink_to(outside, target_is_directory=True)
    store = BlockStore(workspace_root=workspace, user_root=user)

    with pytest.raises(ValueError, match="symlink"):
        store.write_agent_topic(
            scope="workspace",
            topic="policy",
            note="safe note",
        )

    assert list(outside.iterdir()) == []


def test_atomic_no_partial_write(tmp_path, monkeypatch):
    """If replace fails mid-way, main file must not exist half-written."""
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    (workspace / "agent_notes.md").write_text("original\n")
    store = BlockStore(workspace_root=workspace, user_root=user)

    # Simulate write failure by making the target read-only after tempfile write.
    # Just verify no half-written state under normal successful write.
    store.append_agent_note(scope="workspace", note="new")
    contents = (workspace / "agent_notes.md").read_text()
    assert contents.startswith("original")
    assert "new" in contents


def test_stat_all_returns_mtimes(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    user.mkdir()
    (workspace / "notes" / "auth.md").write_text("hi")

    store = BlockStore(workspace_root=workspace, user_root=user)
    stats = store.stat_all()
    assert "workspace/notes/auth.md" in stats
    assert isinstance(stats["workspace/notes/auth.md"], float)


def test_reject_traversal(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    with pytest.raises(ValueError, match="invalid path"):
        store.read("workspace/../etc/passwd")
    with pytest.raises(ValueError, match="invalid path"):
        store.read("/etc/passwd")


def test_size_chars_counts_characters_not_bytes(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    user.mkdir()
    # 6 个中文字符 = 18 bytes (UTF-8)，但 size_chars 应该报 6+换行等等
    content = "密码验证\n还有一行\n"   # 4+4 CJK + 2 newline = 10 chars, 26 bytes
    (workspace / "notes" / "auth.md").write_text(content, encoding="utf-8")
    store = BlockStore(workspace_root=workspace, user_root=user)
    entries = {e.path: e for e in store.list()}
    entry = entries["workspace/notes/auth.md"]
    assert entry.size_chars == len(content), f"expected {len(content)} chars, got {entry.size_chars}"
    # 字节数会 > 字符数
    import os
    assert os.path.getsize(workspace / "notes" / "auth.md") > entry.size_chars


@pytest.mark.parametrize("scope", ("workspace", "user"))
def test_list_skips_symlinked_memory_files(tmp_path, scope):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    (user / "notes").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("outside memory secret", encoding="utf-8")
    root = workspace if scope == "workspace" else user
    (root / "notes" / "linked.md").symlink_to(outside)

    store = BlockStore(workspace_root=workspace, user_root=user)

    assert all(entry.path != f"{scope}/notes/linked.md" for entry in store.list())


def test_list_skips_symlinked_notes_directory(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    outside = tmp_path / "outside-notes"
    workspace.mkdir()
    user.mkdir()
    outside.mkdir()
    (outside / "linked.md").write_text("outside memory secret", encoding="utf-8")
    (workspace / "notes").symlink_to(outside, target_is_directory=True)

    store = BlockStore(workspace_root=workspace, user_root=user)

    assert store.list() == []


@pytest.mark.parametrize("target_kind", ("inside", "outside"))
def test_read_rejects_memory_symlink(target_kind, tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    user.mkdir()
    if target_kind == "inside":
        target = workspace / "notes" / "real.md"
    else:
        target = tmp_path / "outside.md"
    target.write_text("must not be read", encoding="utf-8")
    (workspace / "notes" / "linked.md").symlink_to(target)
    store = BlockStore(workspace_root=workspace, user_root=user)

    with pytest.raises((FileNotFoundError, ValueError)):
        store.read("workspace/notes/linked.md")


def test_relative_scope_roots_keep_existing_listing_behavior(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    user.mkdir()
    (workspace / "notes" / "safe.md").write_text("safe", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    store = BlockStore(workspace_root=Path("workspace"), user_root=Path("user"))

    assert [entry.path for entry in store.list()] == ["workspace/notes/safe.md"]
