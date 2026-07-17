"""Task 25: digest.py — per-tool tool_result summarizers with fallback."""

from pony.context.digest import (
    ToolResultDigest,
    digest_tool_result,
    render_digest_content,
    should_digest,
)
from pony.agent.model_capabilities import TokenAccounting


def test_should_digest_threshold():
    assert not should_digest("short", threshold_tokens=5, token_counter=len)
    assert should_digest("x" * 6, threshold_tokens=5, token_counter=len)


def test_digest_read_file_extracts_summary():
    src = "import os\nfrom pathlib import Path\n\ndef foo():\n    pass\n\nclass Bar:\n    pass\n"
    d = digest_tool_result("read_file", {"path": "a.py"}, src)
    assert d.tool == "read_file"
    assert "a.py" in d.title
    assert any("foo" in b or "Bar" in b for b in d.bullets)


def test_digest_run_shell_extracts_exit_and_lines():
    src = "exit_code: 1\nstdout:\nline1\nline2\nline3\nline4\nstderr:\nerr line\n"
    d = digest_tool_result("run_shell", {"command": "pytest"}, src)
    assert d.tool == "run_shell"
    assert any("exit" in b.lower() for b in d.bullets)


def test_digest_grep_extracts_hits():
    src = "match 1\nmatch 2\nmatch 3\nmatch 4\nmatch 5\nmatch 6\n"
    d = digest_tool_result("grep", {"pattern": "x"}, src)
    assert d.tool == "grep"
    assert len(d.bullets) <= 5


def test_digest_fallback_for_unknown_tool():
    src = "a\nb\nc\nd\ne\n" * 100
    d = digest_tool_result("unknown_tool", {}, src)
    assert d.tool == "unknown_tool"
    assert d.bullets
    assert len(d.bullets) <= 3


def test_digest_source_hash_stable():
    d1 = digest_tool_result("read_file", {"path": "a"}, "same content")
    d2 = digest_tool_result("read_file", {"path": "b"}, "same content")
    assert d1.source_hash == d2.source_hash
    assert d1.content_sha256 == d2.content_sha256


def test_render_content_shape():
    d = ToolResultDigest(
        tool="read_file",
        title="a.py (30 lines)",
        bullets=["import os", "def foo", "class Bar"],
        source_hash="abc123",
        content_sha256="a" * 64,
        raw_result_id="tool_result:abc123",
    )
    text = render_digest_content(d)
    assert "a.py (30 lines)" in text
    assert "import os" in text
    assert f"content_sha256: sha256:{'a' * 64}" in text
    assert "raw_result_id: tool_result:abc123" in text
    assert ".pony/" not in text


def test_render_content_respects_model_token_cap():
    accounting = TokenAccounting()
    d = ToolResultDigest(
        tool="read_file",
        title="very long title " * 500,
        bullets=["very long bullet " * 500 for _ in range(5)],
        source_hash="abc123",
        content_sha256="a" * 64,
        raw_result_id="tool_result:abc123",
    )
    text = render_digest_content(
        d,
        max_tokens=64,
        token_counter=accounting.count_text,
    )
    assert accounting.count_text(text) <= 64
    assert "abc123" in text


def test_summarizer_exception_falls_back():
    # Some args that a per-tool summarizer might choke on — digest_tool_result
    # must still return a valid ToolResultDigest via the fallback path.
    d = digest_tool_result("read_file", {"path": None}, "some content")
    assert d is not None
    assert d.tool == "read_file"
    assert d.source_hash


def test_digest_empty_result():
    d = digest_tool_result("run_shell", {"command": "true"}, "")
    assert d is not None
    assert d.source_hash


def test_should_digest_none_or_empty():
    assert not should_digest("")
    assert not should_digest(None)
