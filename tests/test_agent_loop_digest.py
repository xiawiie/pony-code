"""Task 26: agent_loop tool_result auto-digest.

- Small results (<= threshold) go into messages verbatim.
- Large results (> threshold) are digested; raw body written to
  ``<run_dir>/tool_results/<source_hash>.txt``; message content carries
  the [digest] rendering with a `raw at ...` pointer.
- ``_pico_meta.digest_applied`` and ``source_hash`` reflect what happened.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from pico.agent_loop import _append_tool_result


def _stub_agent(tmp_path, run_id="run1"):
    session_messages = []
    a = MagicMock()
    a.session = {"messages": session_messages, "id": "s1"}
    a.record_message = MagicMock(side_effect=lambda m: session_messages.append(m))
    a.workspace = MagicMock()
    a.workspace.repo_root = str(tmp_path)
    a.current_task_state = SimpleNamespace(run_id=run_id, task_id="t1")
    a.current_run_dir = tmp_path / ".pico" / "runs" / run_id
    a.current_run_dir.mkdir(parents=True, exist_ok=True)
    return a


def test_small_result_stored_inline(tmp_path):
    a = _stub_agent(tmp_path)
    _append_tool_result(
        a,
        tool_use_id="toolu_a",
        content="tiny result",
        tool_name="read_file",
        tool_args={"path": "x"},
    )
    msg = a.session["messages"][-1]
    assert msg["content"][0]["content"] == "tiny result"
    assert msg["_pico_meta"]["digest_applied"] is False


def test_large_result_digested_and_written_to_disk(tmp_path):
    a = _stub_agent(tmp_path)
    big = "x = 1\n" * 500  # > 1200 char
    _append_tool_result(
        a,
        tool_use_id="toolu_b",
        content=big,
        tool_name="read_file",
        tool_args={"path": "big.py"},
    )
    msg = a.session["messages"][-1]
    assert msg["_pico_meta"]["digest_applied"] is True
    source_hash = msg["_pico_meta"]["source_hash"]
    assert source_hash
    raw_files = list((a.current_run_dir / "tool_results").glob(f"{source_hash}.txt"))
    assert len(raw_files) == 1
    assert raw_files[0].read_text(encoding="utf-8") == big
    content_str = msg["content"][0]["content"]
    assert "[digest]" in content_str
    assert source_hash in content_str


def test_large_result_without_run_dir_still_digests(tmp_path):
    """When agent has no run_dir, the digest still applies but raw_path is empty."""
    a = _stub_agent(tmp_path)
    a.current_run_dir = None
    big = "z" * 5000
    _append_tool_result(
        a,
        tool_use_id="toolu_c",
        content=big,
        tool_name="grep",
        tool_args={"pattern": "z"},
    )
    msg = a.session["messages"][-1]
    assert msg["_pico_meta"]["digest_applied"] is True
    content_str = msg["content"][0]["content"]
    assert "[digest]" in content_str
