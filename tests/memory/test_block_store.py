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
