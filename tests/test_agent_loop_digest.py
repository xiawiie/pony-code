"""Task 26: agent_loop tool_result auto-digest.

- Small results (<= threshold) go into messages verbatim.
- Large results (> threshold) are digested; raw body written to
  ``<run_dir>/tool_results/<source_hash>.txt``; message content carries
  the [digest] rendering with a `raw at ...` pointer.
- Returned ``digest_applied`` and ``source_hash`` reflect what happened.
"""

from unittest.mock import MagicMock

from pico.agent_loop import _prepare_tool_result


def _stub_agent(tmp_path, run_id="run1"):
    a = MagicMock()
    a.current_run_dir = tmp_path / ".pico" / "runs" / run_id
    a.current_run_dir.mkdir(parents=True, exist_ok=True)
    return a


def test_small_result_stored_inline(tmp_path):
    a = _stub_agent(tmp_path)
    content, metadata = _prepare_tool_result(
        a,
        content="tiny result",
        tool_name="read_file",
        tool_args={"path": "x"},
    )
    assert content == "tiny result"
    assert metadata == {"digest_applied": False, "source_hash": None}


def test_large_result_digested_and_written_to_disk(tmp_path):
    a = _stub_agent(tmp_path)
    big = "x = 1\n" * 500  # > 1200 char
    content, metadata = _prepare_tool_result(
        a,
        content=big,
        tool_name="read_file",
        tool_args={"path": "big.py"},
    )
    assert metadata["digest_applied"] is True
    source_hash = metadata["source_hash"]
    assert source_hash
    raw_files = list((a.current_run_dir / "tool_results").glob(f"{source_hash}.txt"))
    assert len(raw_files) == 1
    assert raw_files[0].read_text(encoding="utf-8") == big
    assert "[digest]" in content
    assert source_hash in content


def test_large_result_without_run_dir_still_digests(tmp_path):
    """When agent has no run_dir, the digest still applies but raw_path is empty."""
    a = _stub_agent(tmp_path)
    a.current_run_dir = None
    big = "z" * 5000
    content, metadata = _prepare_tool_result(
        a,
        content=big,
        tool_name="grep",
        tool_args={"pattern": "z"},
    )
    assert metadata["digest_applied"] is True
    assert "[digest]" in content


def test_digest_computed_exactly_once(tmp_path, monkeypatch):
    """Task D1: _prepare_tool_result must not run per-tool summarizer twice."""
    import pico.context.digest as digest_mod
    from pico.agent_loop import _prepare_tool_result

    original = digest_mod._digest_read_file
    call_count = {"n": 0}

    def counting_digest_read_file(args, result):
        call_count["n"] += 1
        return original(args, result)

    monkeypatch.setattr(digest_mod, "_digest_read_file", counting_digest_read_file)
    monkeypatch.setitem(digest_mod._DIGESTERS, "read_file", counting_digest_read_file)

    a = MagicMock()
    a.current_run_dir = tmp_path / ".pico" / "runs" / "r1"
    a.current_run_dir.mkdir(parents=True, exist_ok=True)
    a.context_config = {"digest_size_threshold": 100}

    _prepare_tool_result(
        a,
        content="x = 1\n" * 500,
        tool_name="read_file",
        tool_args={"path": "big.py"},
    )
    assert call_count["n"] == 1, f"_digest_read_file called {call_count['n']} times"
