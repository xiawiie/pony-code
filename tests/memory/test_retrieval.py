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
