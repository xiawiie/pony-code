import os
from pathlib import Path

import pytest

import pico.memory.block_store as block_store_module
from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval, tokenize


def test_tokenize_english():
    assert set(tokenize("Hello World bcrypt")) == {"hello", "world", "bcrypt"}


def test_tokenize_cjk_bigrams():
    tokens = tokenize("密码验证")
    # 密码, 码验, 验证
    assert "密码" in tokens
    assert "码验" in tokens
    assert "验证" in tokens


def test_tokenize_mixed():
    tokens = tokenize("使用 bcrypt 加密")
    assert "bcrypt" in tokens
    assert "使用" in tokens         # 同一分段内 CJK 相邻 → bigram
    assert "加密" in tokens         # 同上
    assert "用加" not in tokens     # 跨空白分段, 不产生 bigram


def test_tokenize_no_cross_whitespace_bigram():
    """Regression: whitespace must break CJK bigram grouping."""
    tokens = tokenize("你好 世界")
    assert "你好" in tokens
    assert "世界" in tokens
    assert "好世" not in tokens


def _make_store(tmp_path, files: dict[str, str]):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    user.mkdir()
    for rel, content in files.items():
        full = workspace / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return BlockStore(workspace_root=workspace, user_root=user)


def test_search_finds_english_keyword(tmp_path):
    store = _make_store(tmp_path, {
        "notes/auth.md": "use bcrypt with rounds 10",
        "notes/other.md": "unrelated content",
    })
    r = Retrieval(store)
    hits = r.search("bcrypt")
    assert len(hits) == 1
    assert hits[0].path == "workspace/notes/auth.md"


def test_search_finds_chinese_keyword(tmp_path):
    store = _make_store(tmp_path, {
        "notes/auth.md": "密码验证走 bcrypt",
        "notes/other.md": "无关的内容",
    })
    r = Retrieval(store)
    hits = r.search("密码")
    assert len(hits) >= 1
    assert hits[0].path == "workspace/notes/auth.md"


def test_search_bm25_scores_relevance(tmp_path):
    store = _make_store(tmp_path, {
        "notes/a.md": "bcrypt bcrypt bcrypt scrypt",
        "notes/b.md": "bcrypt is used once",
    })
    r = Retrieval(store)
    hits = r.search("bcrypt")
    assert hits[0].path == "workspace/notes/a.md"
    assert hits[0].score > hits[1].score


def test_search_limit(tmp_path):
    store = _make_store(tmp_path, {
        f"notes/n{i}.md": f"keyword {i}" for i in range(10)
    })
    r = Retrieval(store)
    hits = r.search("keyword", limit=3)
    assert len(hits) == 3


def test_search_snippet_shows_matching_line(tmp_path):
    store = _make_store(tmp_path, {
        "notes/auth.md": "line one\nbcrypt line two\nline three\n",
    })
    r = Retrieval(store)
    hits = r.search("bcrypt")
    assert hits[0].snippets
    assert any("bcrypt" in s for s in hits[0].snippets)


def test_search_no_match(tmp_path):
    store = _make_store(tmp_path, {
        "notes/auth.md": "bcrypt content",
    })
    r = Retrieval(store)
    assert r.search("nonexistent") == []


def test_search_reads_each_candidate_once_without_public_store_reads(
    tmp_path,
    monkeypatch,
):
    store = _make_store(
        tmp_path,
        {
            "notes/a.md": "---\nname: a\ndescription: cache\n---\nsee [[b]]\n",
            "notes/b.md": "---\nname: b\ndescription: related\n---\nbody\n",
            "notes/c.md": "unrelated\n",
        },
    )
    calls = []
    real_read = block_store_module._read_bounded_regular

    def counting_read(path, limit, *, private=False, **kwargs):
        calls.append(Path(path).name)
        return real_read(path, limit, private=private, **kwargs)

    monkeypatch.setattr(block_store_module, "_read_bounded_regular", counting_read)
    monkeypatch.setattr(store, "list", lambda: pytest.fail("search reopened list"))
    monkeypatch.setattr(store, "read", lambda _path: pytest.fail("search reopened file"))

    paths = [hit.path for hit in Retrieval(store).search("cache", limit=1)]

    assert paths == ["workspace/notes/a.md", "workspace/notes/b.md"]
    assert calls == ["a.md", "b.md", "c.md"]


def test_same_retrieval_sees_add_modify_and_delete_on_next_query(tmp_path):
    store = _make_store(tmp_path, {"notes/a.md": "alpha\n"})
    retrieval = Retrieval(store)
    note = store.workspace_root / "notes" / "a.md"

    initial_hits = retrieval.search("alpha")
    assert [hit.path for hit in initial_hits] == [
        "workspace/notes/a.md"
    ]
    assert not hasattr(initial_hits[0], "raw")
    assert not hasattr(retrieval, "_last_snapshot")
    note.write_text("beta\n", encoding="utf-8")
    assert retrieval.search("alpha") == []
    assert [hit.path for hit in retrieval.search("beta")] == [
        "workspace/notes/a.md"
    ]
    added = store.workspace_root / "notes" / "b.md"
    added.write_text("gamma\n", encoding="utf-8")
    assert [hit.path for hit in retrieval.search("gamma")] == [
        "workspace/notes/b.md"
    ]
    note.unlink()
    assert retrieval.search("beta") == []


def test_agent_notes_are_indexed_as_independent_timestamp_blocks(tmp_path):
    store = _make_store(tmp_path, {})
    (store.workspace_root / "agent_notes.md").write_text(
        "- 2026-07-14T01:00:00Z  Keep the deploy procedure unchanged.\n"
        "- 2026-07-15T02:00:00Z  Bcrypt rounds must stay above twelve.\n",
        encoding="utf-8",
    )

    snapshot = Retrieval(store).snapshot()
    logical_paths = [document.path for document in snapshot.documents]
    hits = Retrieval(store).search_snapshot(snapshot, "bcrypt", limit=5)[0]

    assert logical_paths == [
        "workspace/agent_notes.md#entry-1",
        "workspace/agent_notes.md#entry-2",
    ]
    assert [hit.path for hit in hits] == ["workspace/agent_notes.md#entry-2"]


@pytest.mark.parametrize("unsafe_kind", ("symlink", "hardlink", "fifo"))
def test_search_skips_unsafe_candidates(tmp_path, unsafe_kind):
    store = _make_store(tmp_path, {"notes/safe.md": "safe cache\n"})
    outside = tmp_path / "outside.md"
    outside.write_text("outside-canary", encoding="utf-8")
    unsafe = store.workspace_root / "notes" / "unsafe.md"
    if unsafe_kind == "symlink":
        unsafe.symlink_to(outside)
    elif unsafe_kind == "hardlink":
        os.link(outside, unsafe)
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unavailable")
        os.mkfifo(unsafe)

    retrieval = Retrieval(store)

    assert retrieval.search("outside-canary") == []
    assert [hit.path for hit in retrieval.search("cache")] == [
        "workspace/notes/safe.md"
    ]
    assert outside.read_text(encoding="utf-8") == "outside-canary"
