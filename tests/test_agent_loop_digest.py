"""Agent-loop shaping for inline, pageable, and overflow tool results."""

import hashlib
import os
import stat
from unittest.mock import MagicMock

from pony.agent.loop import _prepare_tool_result
from pony.agent.model_capabilities import TokenAccounting
from pony.security.redaction import redact_text
from pony.state.run_store import RunStore


def _stub_agent(tmp_path, run_id="run1"):
    a = MagicMock()
    a.run_store = RunStore(tmp_path / ".pony" / "runs")
    a.current_task_state = run_id
    a.current_run_dir = a.run_store.run_dir(run_id)
    a.redact_text.side_effect = lambda value: value
    a.token_accounting = TokenAccounting()
    a.context_config = {"tool_results": {"inline_tokens": 4096, "digest_tokens": 512}}
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
    assert metadata == {
        "digest_applied": False,
        "source_hash": None,
        "result_view": {"delivery": "inline", "truncated": False},
    }


def test_large_result_preview_is_recoverable_only_after_private_write(tmp_path):
    a = _stub_agent(tmp_path)
    big = "x = 1\n" * 5000  # > 4,096 estimated model tokens
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
    assert "[preview] output truncated" in content
    assert source_hash in content
    assert hashlib.sha256(big.encode("utf-8")).hexdigest() in content
    assert f"raw_result_id=tool_result:{source_hash}" in content
    assert "recoverable=true" in content
    assert "scope=current_run expires=end_of_turn" in content
    assert metadata["result_view"] == {
        "delivery": "preview",
        "truncated": True,
        "reasons": ["tokens"],
        "recoverable": True,
    }
    assert str(a.current_run_dir) not in content


def test_large_tool_result_writes_only_redacted_private_body(tmp_path):
    agent = _stub_agent(tmp_path)
    agent.redact_text.side_effect = lambda value: redact_text(value, env={})
    agent.context_config = {
        "tool_results": {"inline_tokens": 100, "digest_tokens": 512}
    }
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


def test_raw_tool_result_write_failure_omits_reference(tmp_path, monkeypatch):
    agent = _stub_agent(tmp_path)
    agent.context_config = {
        "tool_results": {"inline_tokens": 100, "digest_tokens": 512}
    }
    body = "safe body\n" * 200
    calls = []

    def reject_write(task_state, source_hash, content):
        calls.append((task_state, source_hash, content))
        raise ValueError("raw tool result changed")

    monkeypatch.setattr(agent.run_store, "write_tool_result", reject_write)

    content, metadata = _prepare_tool_result(
        agent,
        content=body,
        tool_name="read_file",
        tool_args={"path": "x"},
    )

    assert calls == [(agent.current_task_state, metadata["source_hash"], body)]
    assert "raw_result_id=" not in content
    assert "recoverable=false" in content
    assert metadata["result_view"]["recoverable"] is False
    assert str(agent.current_run_dir) not in content


def test_raw_tool_result_rejects_hardlink_without_touching_external_inode(
    tmp_path,
):
    agent = _stub_agent(tmp_path)
    agent.context_config = {
        "tool_results": {"inline_tokens": 100, "digest_tokens": 512}
    }
    body = "safe body\n" * 200
    source_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    raw_dir = agent.current_run_dir / "tool_results"
    raw_dir.mkdir(parents=True)
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
    assert "raw_result_id=" not in content
    assert "recoverable=false" in content
    assert str(agent.current_run_dir) not in content


def test_large_result_without_run_dir_is_an_unrecoverable_preview(tmp_path):
    a = _stub_agent(tmp_path)
    a.current_run_dir = None
    big = "z" * 20_000
    content, metadata = _prepare_tool_result(
        a,
        content=big,
        tool_name="grep",
        tool_args={"pattern": "z"},
    )
    assert metadata["digest_applied"] is True
    assert "[preview] output truncated" in content
    assert "content_sha256=sha256:" in content
    assert "raw_result_id=" not in content
    assert "recoverable=false" in content
    assert metadata["result_view"]["recoverable"] is False


def test_page_result_bypasses_preview_and_raw_spill(tmp_path):
    agent = _stub_agent(tmp_path)
    agent.context_config = {
        "tool_results": {"inline_tokens": 100, "digest_tokens": 512}
    }
    page = "[page] metadata\n" + ("x = 1\n" * 500)
    result_view = {
        "delivery": "page",
        "truncated": False,
        "start_line": 1,
        "end_line": 500,
        "total_lines": 1_000,
        "next_start": 501,
        "reasons": ["tokens"],
    }

    content, metadata = _prepare_tool_result(
        agent,
        content=page,
        tool_name="read_file",
        tool_args={"path": "big.py"},
        result_view=result_view,
    )

    assert content == page
    assert metadata["digest_applied"] is False
    assert metadata["result_view"] == result_view
    assert not (agent.current_run_dir / "tool_results").exists()


def test_malformed_page_metadata_cannot_bypass_preview(tmp_path):
    agent = _stub_agent(tmp_path)
    agent.context_config = {
        "tool_results": {"inline_tokens": 100, "digest_tokens": 512}
    }

    content, metadata = _prepare_tool_result(
        agent,
        content="x = 1\n" * 500,
        result_view={
            "delivery": "page",
            "truncated": False,
            "path": "must-not-reach-listener.txt",
        },
    )

    assert "[preview] output truncated" in content
    assert metadata["result_view"]["delivery"] == "preview"
