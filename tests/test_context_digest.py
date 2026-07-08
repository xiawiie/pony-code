"""Task 25: digest.py — per-tool tool_result summarizers with fallback."""

from pico.context.digest import (
    ToolResultDigest,
    digest_tool_result,
    render_digest_content,
    should_digest,
)


def test_should_digest_threshold():
    assert not should_digest("short")
    assert should_digest("x" * 1201)


def test_digest_read_file_extracts_summary():
    src = "import os\nfrom pathlib import Path\n\ndef foo():\n    pass\n\nclass Bar:\n    pass\n"
    d = digest_tool_result("read_file", {"path": "a.py"}, src, raw_path="raw/abc.txt")
    assert d.tool == "read_file"
    assert "a.py" in d.title
    assert any("foo" in b or "Bar" in b for b in d.bullets)


def test_digest_run_shell_extracts_exit_and_lines():
    src = "exit_code: 1\nstdout:\nline1\nline2\nline3\nline4\nstderr:\nerr line\n"
    d = digest_tool_result("run_shell", {"command": "pytest"}, src, raw_path="raw/x.txt")
    assert d.tool == "run_shell"
    assert any("exit" in b.lower() for b in d.bullets)


def test_digest_grep_extracts_hits():
    src = "match 1\nmatch 2\nmatch 3\nmatch 4\nmatch 5\nmatch 6\n"
    d = digest_tool_result("grep", {"pattern": "x"}, src, raw_path="raw/y.txt")
    assert d.tool == "grep"
    assert len(d.bullets) <= 5


def test_digest_fallback_for_unknown_tool():
    src = "a\nb\nc\nd\ne\n" * 100
    d = digest_tool_result("unknown_tool", {}, src, raw_path="raw/z.txt")
    assert d.tool == "unknown_tool"
    assert d.bullets
    assert len(d.bullets) <= 3


def test_digest_source_hash_stable():
    d1 = digest_tool_result("read_file", {"path": "a"}, "same content", raw_path="p")
    d2 = digest_tool_result("read_file", {"path": "b"}, "same content", raw_path="q")
    assert d1.source_hash == d2.source_hash


def test_render_content_shape():
    d = ToolResultDigest(
        tool="read_file",
        title="a.py (30 lines)",
        bullets=["import os", "def foo", "class Bar"],
        source_hash="abc123",
        raw_path=".pico/runs/x/tool_results/abc123.txt",
    )
    text = render_digest_content(d)
    assert "a.py (30 lines)" in text
    assert "import os" in text
    assert ".pico/runs/x/tool_results/abc123.txt" in text


def test_summarizer_exception_falls_back():
    # Some args that a per-tool summarizer might choke on — digest_tool_result
    # must still return a valid ToolResultDigest via the fallback path.
    d = digest_tool_result("read_file", {"path": None}, "some content", raw_path="p")
    assert d is not None
    assert d.tool == "read_file"
    assert d.source_hash


def test_digest_empty_result():
    d = digest_tool_result("run_shell", {"command": "true"}, "", raw_path="r/e.txt")
    assert d is not None
    assert d.source_hash


def test_should_digest_none_or_empty():
    assert not should_digest("")
    assert not should_digest(None)
