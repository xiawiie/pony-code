"""Task 11 · memory system 结构不变量 (INV-*).

INV-1: agent_notes.md 只能通过 append_agent_note 追加（外部 vim 也 OK）.
INV-2: append_agent_note 不能写到 notes/*.md 目录.
INV-3: list() 覆盖 workspace + user 两个 scope 的 notes/ 与 agent_notes.md.
INV-4: 追加使用原子重命名，不会留下半行.
INV-5: agent_notes.md 超过 8000 字符时, 每个 store 实例仅 stderr 告警一次.
"""

import pytest


def _store(tmp_path):
    from pico.memory.block_store import BlockStore
    ws = tmp_path / "workspace"
    us = tmp_path / "user"
    ws.mkdir()
    us.mkdir()
    return BlockStore(workspace_root=ws, user_root=us)


def test_inv_1_agent_notes_appended_by_save(tmp_path):
    store = _store(tmp_path)
    store.append_agent_note(scope="workspace", note="one")
    contents = (tmp_path / "workspace" / "agent_notes.md").read_text(encoding="utf-8")
    assert "one" in contents


def test_inv_2_append_never_writes_notes_dir(tmp_path):
    store = _store(tmp_path)
    store.append_agent_note(scope="workspace", note="hi")
    notes_dir = tmp_path / "workspace" / "notes"
    if notes_dir.exists():
        for md in notes_dir.rglob("*.md"):
            assert "hi" not in md.read_text(encoding="utf-8")


def test_inv_3_list_covers_both_scopes(tmp_path):
    from pico.memory.block_store import BlockStore
    (tmp_path / "workspace" / "notes").mkdir(parents=True)
    (tmp_path / "user" / "notes").mkdir(parents=True)
    (tmp_path / "workspace" / "notes" / "w.md").write_text("w")
    (tmp_path / "user" / "notes" / "u.md").write_text("u")
    store = BlockStore(
        workspace_root=tmp_path / "workspace",
        user_root=tmp_path / "user",
    )
    store.append_agent_note(scope="workspace", note="wa")
    store.append_agent_note(scope="user", note="ua")
    paths = {e.path for e in store.list()}
    assert paths >= {
        "workspace/notes/w.md",
        "user/notes/u.md",
        "workspace/agent_notes.md",
        "user/agent_notes.md",
    }


def test_inv_4_atomic_writes_produce_no_half_lines(tmp_path):
    store = _store(tmp_path)
    for _ in range(50):
        store.append_agent_note(scope="workspace", note="x")
    contents = (tmp_path / "workspace" / "agent_notes.md").read_text(encoding="utf-8")
    lines = [line for line in contents.splitlines() if line.strip()]
    assert len(lines) == 50
    for line in lines:
        assert line.startswith("- ") and " x" in line


def test_inv_5_size_warning_emitted_once_per_store(tmp_path, capsys):
    store = _store(tmp_path)
    note = "x" * 200
    total = 0
    while total < 8200:
        total = store.append_agent_note(scope="workspace", note=note)
    first = capsys.readouterr()
    assert "8000" in first.err or "8" in first.err or "review" in first.err.lower()
    store.append_agent_note(scope="workspace", note=note)
    second = capsys.readouterr()
    assert "8000" not in second.err and "review" not in second.err.lower()


def test_inv_5_size_warning_silent_below_threshold(tmp_path, capsys):
    store = _store(tmp_path)
    store.append_agent_note(scope="workspace", note="tiny")
    captured = capsys.readouterr()
    assert captured.err == ""
