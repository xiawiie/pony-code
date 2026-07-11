"""Task 26: agent_loop tool_result auto-digest.

- Small results (<= threshold) go into messages verbatim.
- Large results (> threshold) are digested; raw body written to
  ``<run_dir>/tool_results/<source_hash>.txt``; message content carries
  the [digest] rendering with a `raw at ...` pointer.
- Returned ``digest_applied`` and ``source_hash`` reflect what happened.
"""

import hashlib
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pico.agent_loop as agent_loop_module
from pico.agent_loop import _prepare_tool_result
from pico.security import redact_text


def _stub_agent(tmp_path, run_id="run1"):
    a = MagicMock()
    a.current_run_dir = tmp_path / ".pico" / "runs" / run_id
    a.current_run_dir.mkdir(parents=True, exist_ok=True)
    a.redact_text.side_effect = lambda value: value
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


def test_large_tool_result_writes_only_redacted_private_body(tmp_path):
    agent = _stub_agent(tmp_path)
    agent.redact_text.side_effect = lambda value: redact_text(value, env={})
    agent.context_config = {"digest_size_threshold": 100}
    secret = "github_pat_A123456789012345678901234567890"

    content, metadata = _prepare_tool_result(
        agent,
        content=(secret + "\n") * 100,
        tool_name="read_file",
        tool_args={"path": "x"},
    )

    raw_file = next((agent.current_run_dir / "tool_results").glob("*.txt"))
    assert secret not in raw_file.read_text(encoding="utf-8")
    assert secret not in content
    assert raw_file.stem == metadata["source_hash"]
    if os.name == "posix":
        assert stat.S_IMODE(raw_file.stat().st_mode) == 0o600


def test_raw_tool_result_inode_swap_does_not_truncate_replacement(
    tmp_path,
    monkeypatch,
):
    agent = _stub_agent(tmp_path)
    agent.context_config = {"digest_size_threshold": 100}
    body = "safe body\n" * 200
    source_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    raw_dir = agent.current_run_dir / "tool_results"
    raw_dir.mkdir()
    raw_path = raw_dir / f"{source_hash}.txt"
    raw_path.write_text("original\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("replacement\n", encoding="utf-8")
    real_open = agent_loop_module.os.open
    swapped = False

    def swap_before_open(path, flags, mode=0o777):
        nonlocal swapped
        if not swapped and Path(path) == raw_path:
            swapped = True
            raw_path.unlink()
            os.link(outside, raw_path)
        return real_open(path, flags, mode)

    monkeypatch.setattr(agent_loop_module.os, "open", swap_before_open)

    content, metadata = _prepare_tool_result(
        agent,
        content=body,
        tool_name="read_file",
        tool_args={"path": "x"},
    )

    assert swapped is True
    assert metadata["source_hash"] == source_hash
    assert outside.read_text(encoding="utf-8") == "replacement\n"
    assert raw_path.read_text(encoding="utf-8") == "replacement\n"
    assert "(raw at " not in content


def test_raw_tool_result_rejects_hardlink_without_touching_external_inode(
    tmp_path,
):
    agent = _stub_agent(tmp_path)
    agent.context_config = {"digest_size_threshold": 100}
    body = "safe body\n" * 200
    source_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    raw_dir = agent.current_run_dir / "tool_results"
    raw_dir.mkdir()
    outside = tmp_path / "outside-raw.txt"
    outside.write_text("outside\n", encoding="utf-8")
    outside.chmod(0o644)
    os.link(outside, raw_dir / f"{source_hash}.txt")

    content, metadata = _prepare_tool_result(
        agent,
        content=body,
        tool_name="read_file",
        tool_args={"path": "x"},
    )

    assert metadata["source_hash"] == source_hash
    assert outside.read_text(encoding="utf-8") == "outside\n"
    if os.name == "posix":
        assert stat.S_IMODE(outside.stat().st_mode) == 0o644
    assert "(raw at " not in content


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
    a.redact_text.side_effect = lambda value: value

    _prepare_tool_result(
        a,
        content="x = 1\n" * 500,
        tool_name="read_file",
        tool_args={"path": "big.py"},
    )
    assert call_count["n"] == 1, f"_digest_read_file called {call_count['n']} times"
