"""Offline tests for the live-e2e trace and assertion harness.

These tests never enter the normal ``main`` path or create a provider client.
"""

import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from benchmarks.live_e2e import run_live_session
from benchmarks.live_e2e.run_live_session import (
    Assertion,
    AssertionEngine,
    Reporter,
    RunConfig,
    TurnResult,
)


def _config(**overrides):
    defaults = dict(
        provider="deepseek",
        model="test-model",
        max_provider_calls=15,
        max_total_tokens=200_000,
        timeout_seconds=300,
        reset=False,
        verbose=False,
    )
    defaults.update(overrides)
    return RunConfig(**defaults)


def _engine(**overrides):
    return AssertionEngine(_config(**overrides))


def test_active_artifact_scan_detects_secret_and_mode_failures(tmp_path):
    secret = "ghp_" + "A" * 32
    pico_root = tmp_path / ".pico"
    before = run_live_session.snapshot_private_artifacts(pico_root)
    run_dir = pico_root / "runs" / "run-test"
    run_dir.mkdir(parents=True)
    artifact = run_dir / "trace.jsonl"
    artifact.write_text(secret, encoding="utf-8")
    artifact.chmod(0o644)
    if os.name == "posix":
        for directory in (pico_root, pico_root / "runs", run_dir):
            directory.chmod(0o700)

    result = run_live_session.scan_active_private_artifacts(
        pico_root,
        before,
        forbidden_values=(secret,),
    )

    assert result["files_scanned"] == 1
    assert result["secret_hits"] == [".pico/runs/run-test/trace.jsonl"]
    if os.name == "posix":
        assert result["mode_failures"] == [
            ".pico/runs/run-test/trace.jsonl:0644"
        ]


def test_active_artifact_scan_ignores_unchanged_baseline_file(tmp_path):
    pico_root = tmp_path / ".pico"
    session = pico_root / "sessions" / "old.json"
    session.parent.mkdir(parents=True)
    session.write_text("old", encoding="utf-8")
    if os.name == "posix":
        pico_root.chmod(0o700)
        session.parent.chmod(0o700)
        session.chmod(0o600)
    before = run_live_session.snapshot_private_artifacts(pico_root)

    result = run_live_session.scan_active_private_artifacts(
        pico_root,
        before,
        forbidden_values=("old",),
    )

    assert result == {
        "files_scanned": 0,
        "secret_hits": [],
        "mode_failures": [],
    }


def test_fixture_backup_is_private_before_fixture_mutation(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX permission assertion")
    original = b"setting = 'ordinary-value'\n"
    (tmp_path / "pico.toml").write_bytes(original)
    seed = tmp_path / "seed.md"
    seed.write_text("safe seed\n", encoding="utf-8")
    fixture = run_live_session.FixtureManager(tmp_path)
    fixture._seed_source = seed

    fixture.__enter__()
    try:
        backup = tmp_path / run_live_session.BACKUP_REL
        assert backup.read_bytes() == original
        assert backup.stat().st_mode & 0o777 == 0o600
        assert backup.parent.stat().st_mode & 0o777 == 0o700
    finally:
        fixture.__exit__(None, None, None)


def test_fixture_rejects_selected_key_before_backup(tmp_path):
    secret = "ghp_" + "K" * 32
    original = ("setting = '" + secret + "'\n").encode()
    (tmp_path / "pico.toml").write_bytes(original)
    seed = tmp_path / "seed.md"
    seed.write_text("safe seed\n", encoding="utf-8")
    fixture = run_live_session.FixtureManager(
        tmp_path,
        forbidden_values=(secret,),
    )
    fixture._seed_source = seed

    with pytest.raises(
        run_live_session.SensitiveDataBlockedError,
        match="fixture backup",
    ):
        fixture.__enter__()

    assert (tmp_path / "pico.toml").read_bytes() == original
    assert not (tmp_path / run_live_session.BACKUP_REL).exists()


def test_fixture_rejects_unlisted_high_confidence_secret_before_backup(
    tmp_path,
):
    secret = "ghp_" + "Z" * 32
    original = ("setting = '" + secret + "'\n").encode()
    (tmp_path / "pico.toml").write_bytes(original)
    fixture = run_live_session.FixtureManager(
        tmp_path,
        forbidden_values=("different-selected-provider-key",),
    )

    with pytest.raises(
        run_live_session.SensitiveDataBlockedError,
        match="fixture backup",
    ):
        fixture.__enter__()

    assert (tmp_path / "pico.toml").read_bytes() == original
    assert not (tmp_path / run_live_session.BACKUP_REL).exists()


def test_parse_args_selects_exactly_one_supported_provider(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_live_session", "--provider", "deepseek"],
    )

    config = run_live_session.parse_args()

    assert config.provider == "deepseek"


@pytest.mark.parametrize("provider", ["deepseek", "anthropic"])
def test_project_env_uses_canonical_selected_provider_settings(tmp_path, provider):
    prefix = provider.upper()
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                f"PICO_{prefix}_API_KEY=sentinel-{provider}",
                f"PICO_{prefix}_MODEL={provider}-test-model",
                f"PICO_{prefix}_API_BASE=https://{provider}.example.invalid/anthropic",
            ]
        ),
        encoding="utf-8",
    )

    with patch.dict(os.environ, {}, clear=True):
        settings = run_live_session.provider_settings(
            provider,
            project_env=run_live_session.read_project_env(tmp_path),
            process_env={},
        )

    assert settings == {
        "api_key": f"sentinel-{provider}",
        "model": f"{provider}-test-model",
        "base_url": f"https://{provider}.example.invalid/anthropic",
    }


def test_main_reads_project_env_before_parse_args_on_reset_only_path(tmp_path, monkeypatch):
    events = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        run_live_session,
        "read_project_env",
        lambda root: events.append(("read", root)) or {},
    )
    monkeypatch.setattr(
        run_live_session,
        "parse_args",
        lambda **_kwargs: events.append(("parse", None)) or _config(reset=True),
    )
    monkeypatch.setattr(
        run_live_session,
        "do_reset",
        lambda root: events.append(("reset", root)) or 0,
    )

    assert run_live_session.main() == 0
    assert events == [("read", tmp_path), ("parse", None), ("reset", tmp_path)]


def test_main_constructs_live_pico_with_only_read_file(tmp_path, monkeypatch):
    import pico.runtime
    import pico.session_store
    import pico.workspace

    captured = {}

    def capture_pico(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("construction captured")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_live_session, "parse_args", lambda **_kwargs: _config())
    monkeypatch.setattr(run_live_session, "check_env", lambda _config, **_kwargs: None)
    monkeypatch.setattr(run_live_session, "verify_pico_repo", lambda _root: None)
    monkeypatch.setattr(
        run_live_session,
        "warn_if_dirty_working_tree",
        lambda _root: None,
    )
    monkeypatch.setattr(
        run_live_session,
        "FixtureManager",
        lambda _root, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(run_live_session, "make_live_client", lambda _config, **_kwargs: object())
    monkeypatch.setattr(pico.workspace.WorkspaceContext, "build", lambda _root: object())
    monkeypatch.setattr(pico.session_store, "SessionStore", lambda _root: object())
    monkeypatch.setattr(pico.runtime, "Pico", capture_pico)

    assert run_live_session.main() == 4
    assert captured["allowed_tools"] == ("read_file",)
    assert captured["max_steps"] == 2


def test_read_turn_trace_aggregates_every_model_turn(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                {
                    "event": "model_turn",
                    "request_metadata": {"system_prefix_hash": "k", "messages_count": 1},
                    "completion_usage": {"input_tokens": 10, "output_tokens": 2},
                },
                {
                    "event": "action_decoded",
                    "action_type": "tool",
                    "origin": "native_tool_use",
                },
                {
                    "event": "model_turn",
                    "request_metadata": {"system_prefix_hash": "k", "messages_count": 3},
                    "completion_usage": {
                        "input_tokens": 20,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 8,
                    },
                },
            ]
        ),
        encoding="utf-8",
    )

    captured = run_live_session.read_turn_trace(trace)

    assert captured["provider_calls"] == 2
    assert captured["usage"] == {
        "input_tokens": 30,
        "output_tokens": 6,
        "total_tokens": 0,
        "cached_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 8,
    }
    assert captured["usage_complete"] is True
    assert captured["request_metadata"] == [
        {"system_prefix_hash": "k", "messages_count": 1},
        {"system_prefix_hash": "k", "messages_count": 3},
    ]
    assert captured["action_origins"] == ["native_tool_use"]
    assert captured["system_prefix_hashes"] == ["k", "k"]


def test_read_turn_trace_does_not_accept_a_nonstring_cache_key(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(
            {
                "event": "model_turn",
                "request_metadata": {"system_prefix_hash": None},
                "completion_usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ),
        encoding="utf-8",
    )

    captured = run_live_session.read_turn_trace(trace)

    assert captured["system_prefix_hashes"] == [""]


@pytest.mark.parametrize("contents", [None, '{"event":'])
def test_read_turn_trace_marks_missing_or_malformed_usage_unknown(tmp_path, contents):
    trace = tmp_path / "trace.jsonl"
    if contents is not None:
        trace.write_text(contents, encoding="utf-8")

    captured = run_live_session.read_turn_trace(trace)

    assert captured["provider_calls"] == 0
    assert captured["usage_complete"] is False


def test_read_turn_trace_marks_non_utf8_artifact_usage_unknown(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_bytes(b"\xff")

    captured = run_live_session.read_turn_trace(trace)

    assert captured["provider_calls"] == 0
    assert captured["usage_complete"] is False


def test_read_run_terminal_status_uses_each_persisted_artifact(tmp_path):
    from pico.run_store import RunStore

    run_store = RunStore(tmp_path)
    task_state = SimpleNamespace(run_id="run-1")
    run_store.task_state_path(task_state).parent.mkdir(parents=True)
    run_store.task_state_path(task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": "final_answer_returned"}),
        encoding="utf-8",
    )
    run_store.report_path(task_state).write_text(
        json.dumps({"status": "stopped", "stop_reason": "step_limit_reached"}),
        encoding="utf-8",
    )
    run_store.trace_path(task_state).write_text(
        json.dumps({"event": "run_finished"}) + "\n",
        encoding="utf-8",
    )

    assert run_live_session.read_run_terminal_status(run_store, task_state) == (
        "run-1",
        True,
        True,
        True,
    )

    run_store.report_path(task_state).write_text(
        json.dumps({"status": "failed", "stop_reason": ""}),
        encoding="utf-8",
    )
    _, _, report_terminal, _ = run_live_session.read_run_terminal_status(
        run_store,
        task_state,
    )
    assert report_terminal is False


@pytest.mark.parametrize("stop_reason", [None, 0, True, " "])
def test_read_run_terminal_status_rejects_nonstring_or_blank_stop_reason(
    tmp_path,
    stop_reason,
):
    from pico.run_store import RunStore

    run_store = RunStore(tmp_path)
    task_state = SimpleNamespace(run_id="run-invalid-reason")
    run_store.task_state_path(task_state).parent.mkdir(parents=True)
    run_store.task_state_path(task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": "done"}),
        encoding="utf-8",
    )
    run_store.report_path(task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": stop_reason}),
        encoding="utf-8",
    )
    run_store.trace_path(task_state).write_text(
        json.dumps({"event": "run_finished"}) + "\n",
        encoding="utf-8",
    )

    _, _, report_terminal, _ = run_live_session.read_run_terminal_status(
        run_store,
        task_state,
    )

    assert report_terminal is False


@pytest.mark.parametrize(
    ("artifact", "expected_terminal_flags"),
    [
        ("task_state", (False, True, True)),
        ("report", (True, False, True)),
        ("trace", (True, True, False)),
    ],
)
def test_read_run_terminal_status_keeps_other_artifact_evidence(
    tmp_path,
    artifact,
    expected_terminal_flags,
):
    from pico.run_store import RunStore

    run_store = RunStore(tmp_path)
    task_state = SimpleNamespace(run_id="run-one-bad-artifact")
    run_store.task_state_path(task_state).parent.mkdir(parents=True)
    run_store.task_state_path(task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": "done"}),
        encoding="utf-8",
    )
    run_store.report_path(task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": "done"}),
        encoding="utf-8",
    )
    run_store.trace_path(task_state).write_text(
        json.dumps({"event": "run_finished"}) + "\n",
        encoding="utf-8",
    )
    {
        "task_state": run_store.task_state_path(task_state),
        "report": run_store.report_path(task_state),
        "trace": run_store.trace_path(task_state),
    }[artifact].write_text("{", encoding="utf-8")

    _, task_terminal, report_terminal, trace_terminal = (
        run_live_session.read_run_terminal_status(run_store, task_state)
    )

    assert (task_terminal, report_terminal, trace_terminal) == expected_terminal_flags


def test_turn_runner_does_not_reuse_previous_run_evidence_after_pre_run_failure(
    tmp_path,
):
    from pico.run_store import RunStore

    run_store = RunStore(tmp_path)
    previous_task_state = SimpleNamespace(run_id="previous-run")
    run_store.task_state_path(previous_task_state).parent.mkdir(parents=True)
    run_store.task_state_path(previous_task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": "done"}),
        encoding="utf-8",
    )
    run_store.report_path(previous_task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": "done"}),
        encoding="utf-8",
    )
    run_store.trace_path(previous_task_state).write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "model_turn",
                        "request_metadata": {"system_prefix_hash": "old-key"},
                        "completion_usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                ),
                json.dumps({"event": "run_finished"}),
            ]
        ),
        encoding="utf-8",
    )

    def fail_before_starting_new_run(_prompt):
        raise OSError("initial user save failed")

    pico = SimpleNamespace(
        session={"messages": []},
        model_client=SimpleNamespace(calls=[]),
        run_store=run_store,
        current_task_state=previous_task_state,
        ask=fail_before_starting_new_run,
    )

    result = run_live_session.TurnRunner(pico, _config()).run_turn(
        2,
        "new request",
        "must not reuse old evidence",
    )

    assert result.error == "OSError: initial user save failed"
    assert result.provider_call_count_this_turn == 0
    assert result.usage_complete is False
    assert result.metadata == {}
    assert result.actual_user_contents == ()
    assert result.run_id == ""
    assert not result.task_state_terminal
    assert not result.report_terminal
    assert not result.trace_terminal


def test_turn_runner_uses_first_trace_call_as_current_turn_evidence(tmp_path):
    from pico.run_store import RunStore

    run_store = RunStore(tmp_path)
    previous_task_state = SimpleNamespace(run_id="previous-run")
    current_task_state = SimpleNamespace(run_id="current-run")
    first_metadata = {"messages_count": 3, "system_prefix_hash": "stable-key"}
    second_metadata = {"messages_count": 4, "system_prefix_hash": "stable-key"}
    run_store.task_state_path(current_task_state).parent.mkdir(parents=True)
    run_store.task_state_path(current_task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": "done"}),
        encoding="utf-8",
    )
    run_store.report_path(current_task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": "done"}),
        encoding="utf-8",
    )
    run_store.trace_path(current_task_state).write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "model_turn",
                        "request_metadata": first_metadata,
                        "completion_usage": {"input_tokens": 2, "output_tokens": 1},
                    }
                ),
                json.dumps(
                    {
                        "event": "action_decoded",
                        "origin": "native_tool_use",
                    }
                ),
                json.dumps(
                    {
                        "event": "model_turn",
                        "request_metadata": second_metadata,
                        "completion_usage": {"input_tokens": 3, "output_tokens": 2},
                    }
                ),
                json.dumps({"event": "run_finished"}),
            ]
        ),
        encoding="utf-8",
    )

    pico = SimpleNamespace(
        session={"messages": []},
        model_client=SimpleNamespace(calls=[{"last_user_content": "old prompt"}]),
        run_store=run_store,
        current_task_state=previous_task_state,
    )

    def start_current_run(_prompt):
        pico.current_task_state = current_task_state
        pico.model_client.calls.extend(
            [
                {"last_user_content": "first current prompt"},
                {"last_user_content": "second current prompt"},
            ]
        )
        return "ok"

    pico.ask = start_current_run
    result = run_live_session.TurnRunner(pico, _config()).run_turn(
        2,
        "new request",
        "trace truth",
    )

    assert result.metadata == first_metadata
    assert result.provider_input_messages_len == 3
    assert result.request_metadata_by_call == (first_metadata, second_metadata)
    assert result.usage == {
        "input_tokens": 5,
        "output_tokens": 3,
        "total_tokens": 0,
        "cached_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    assert result.system_prefix_hashes == ("stable-key", "stable-key")
    assert result.action_origins == ("native_tool_use",)
    assert result.actual_user_contents == (
        "first current prompt",
        "second current prompt",
    )
    assert result.run_id == "current-run"
    assert result.task_state_terminal
    assert result.report_terminal
    assert result.trace_terminal


def _canonical_session_messages():
    return [
        {"role": "user", "content": "question", "_pico_meta": {}},
        {"role": "assistant", "content": "answer", "_pico_meta": {}},
    ]


def _pico_stub_with_persisted_v3(tmp_path):
    session = {
        "record_type": "session",
        "format_version": 1,
        "messages": _canonical_session_messages(),
    }
    session_path = tmp_path / "session.json"
    session_path.write_text(json.dumps(session), encoding="utf-8")
    return SimpleNamespace(
        session=session,
        session_path=session_path,
        model_client=SimpleNamespace(
            calls=[{"payload_secret_clean": True}]
        ),
    )


def _turn_result_stub(**overrides):
    defaults = dict(
        turn=1,
        user_prompt="上次讨论过 cache invariant 的问题",
        expected_behavior="recall_triggered",
        final_answer="ok",
        metadata={
            "intent": {"name": "recall", "matched_keyword": "上次", "matched_reason": ""},
            "injection_tokens": {"recalled_memory": 42, "workspace_state": 10},
            "recall.error_count": 0,
        },
        session_message_count_before=0,
        session_message_count_after=2,
        provider_call_count_this_turn=1,
        duration_ms=100,
        usage={"input_tokens": 10, "output_tokens": 5},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=1,
        current_user_content=(
            "<system-reminder><pico:recalled_memory path=\"workspace/agent/cache-invariant.md\">"
            "content</pico:recalled_memory></system-reminder>\n上次讨论过 cache invariant 的问题"
        ),
        usage_complete=True,
        request_metadata_by_call=({},),
        system_prefix_hashes=("cache-key",),
        action_origins=("provider_text",),
        actual_user_contents=("prompt",),
        run_id="run-1",
        task_state_terminal=True,
        report_terminal=True,
        trace_terminal=True,
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def test_check_turn_1_recall_passes_on_valid_metadata():
    engine = _engine()
    result = _turn_result_stub()
    asserts = engine.check_turn_1_recall(result)
    # All 6 required assertions present and passed
    assert len(asserts) == 6
    assert all(a.passed for a in asserts), [a for a in asserts if not a.passed]


def test_check_turn_1_recall_fails_when_intent_not_recall():
    engine = _engine()
    result = _turn_result_stub(metadata={
        "intent": {"name": "default", "matched_keyword": "", "matched_reason": ""},
        "injection_tokens": {"recalled_memory": 42},
        "recall.error_count": 0,
    })
    asserts = engine.check_turn_1_recall(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "intent_name_recall" for a in failed)


def test_check_turn_1_recall_fails_when_no_recall_block_rendered():
    engine = _engine()
    result = _turn_result_stub(current_user_content="上次讨论过什么", metadata={
        "intent": {"name": "recall", "matched_keyword": "上次", "matched_reason": ""},
        "injection_tokens": {"recalled_memory": 0},
        "recall.error_count": 0,
    })
    asserts = engine.check_turn_1_recall(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "recalled_memory_block_present" for a in failed)


def test_check_turn_1_recall_fails_when_recall_error_nonzero():
    engine = _engine()
    result = _turn_result_stub(metadata={
        "intent": {"name": "recall", "matched_keyword": "上次", "matched_reason": ""},
        "injection_tokens": {"recalled_memory": 42},
        "recall.error_count": 3,
    })
    asserts = engine.check_turn_1_recall(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "recall_error_count_zero" for a in failed)


def test_assertion_is_frozen():
    a = Assertion(name="x", passed=True, expected="e", actual="a")
    import pytest
    with pytest.raises(Exception):
        a.name = "y"


def test_dispatch_routes_turn_1_to_recall_check():
    engine = _engine()
    result = _turn_result_stub()
    asserts = engine.dispatch(1, result, pico=MagicMock(), all_results=[result])
    assert len(asserts) == 6

def _turn_2_result_stub(**overrides):
    """Session state includes a tool_result message with digest applied."""
    defaults = dict(
        turn=2,
        user_prompt="读一下 pico/runtime.py",
        expected_behavior="digest_applied",
        final_answer="ok",
        metadata={"injection_tokens": {"recalled_memory": 1}},
        session_message_count_before=2,
        session_message_count_after=6,
        provider_call_count_this_turn=2,
        duration_ms=100,
        usage={},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=6,
        current_user_content="",
        usage_complete=True,
        request_metadata_by_call=(
            {"injection_tokens": {"recalled_memory": 1}},
            {"injection_tokens": {"recalled_memory": 1}},
        ),
        system_prefix_hashes=("cache-key", "cache-key"),
        action_origins=("native_tool_use",),
        actual_user_contents=(
            "<system-reminder>context</system-reminder>\n读一下 pico/runtime.py",
            "<system-reminder>context</system-reminder>\n读一下 pico/runtime.py",
        ),
        run_id="run-2",
        task_state_terminal=True,
        report_terminal=True,
        trace_terminal=True,
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def _pico_stub_with_digested_message(raw_body: str, raw_dir: Path, source_hash: str = "abc12345"):
    """Build a MagicMock pico whose session has a digested tool_result at the tail."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / f"{source_hash}.txt"
    raw_file.write_text(raw_body, encoding="utf-8")

    pico = MagicMock()
    pico.session = {
        "messages": [
            {"role": "user", "content": "read"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1",
                             "content": f"[digest] runtime.py (900 lines)\n- import\n(raw at {raw_file})"}],
                "_pico_meta": {"digest_applied": True, "source_hash": source_hash, "tool_use_id": "t1"},
            },
        ]
    }
    return pico, raw_file


def test_check_turn_2_digest_passes_on_valid_state(tmp_path):
    engine = _engine()
    raw_body = "x" * 5000
    pico, raw_file = _pico_stub_with_digested_message(raw_body, tmp_path / "runs" / "tool_results")
    result = _turn_2_result_stub()
    asserts = engine.check_turn_2_digest(result, pico)
    assert len(asserts) == 12
    assert all(a.passed for a in asserts), [(a.name, a.actual) for a in asserts if not a.passed]


def test_check_turn_2_allows_plain_prompt_when_nothing_was_injected(tmp_path):
    pico, _ = _pico_stub_with_digested_message(
        "x" * 5000,
        tmp_path / "runs",
    )
    prompt = "读一下 pico/runtime.py"
    result = _turn_2_result_stub(
        metadata={"injection_tokens": {"recalled_memory": 0}},
        request_metadata_by_call=(
            {"injection_tokens": {"recalled_memory": 0}},
            {"injection_tokens": {"recalled_memory": 0}},
        ),
        actual_user_contents=(prompt, prompt),
    )

    assertions = _engine().check_turn_2_digest(result, pico)

    assert next(
        assertion
        for assertion in assertions
        if assertion.name == "injected_user_prompt_reaches_every_provider_call"
    ).passed


def test_check_turn_2_fails_when_later_injected_call_lacks_reminder(tmp_path):
    pico, _ = _pico_stub_with_digested_message(
        "x" * 5000,
        tmp_path / "runs",
    )
    prompt = "读一下 pico/runtime.py"
    result = _turn_2_result_stub(
        metadata={"injection_tokens": {"recalled_memory": 0}},
        request_metadata_by_call=(
            {"injection_tokens": {"recalled_memory": 0}},
            {"injection_tokens": {"recalled_memory": 12}},
        ),
        actual_user_contents=(prompt, prompt),
    )

    assertions = _engine().check_turn_2_digest(result, pico)

    assert not next(
        assertion
        for assertion in assertions
        if assertion.name == "injected_user_prompt_reaches_every_provider_call"
    ).passed


def test_check_turn_2_requires_complete_native_trace_evidence(tmp_path):
    pico, _ = _pico_stub_with_digested_message("x" * 5000, tmp_path / "runs")
    result = _turn_2_result_stub(
        action_origins=("provider_text",),
        usage_complete=False,
        actual_user_contents=("plain prompt",),
        system_prefix_hashes=("",),
    )

    assertions = _engine().check_turn_2_digest(result, pico)

    failed = {assertion.name for assertion in assertions if not assertion.passed}
    assert {
        "native_tool_action_observed",
        "turn_usage_complete",
        "injected_user_prompt_reaches_every_provider_call",
        "system_prefix_hashes_cover_every_provider_call",
    } <= failed


def test_check_turn_2_digest_fails_when_no_digest_applied(tmp_path):
    engine = _engine()
    pico = MagicMock()
    pico.session = {
        "messages": [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "raw output"}],
             "_pico_meta": {"digest_applied": False, "tool_use_id": "t1"}},
        ]
    }
    asserts = engine.check_turn_2_digest(_turn_2_result_stub(), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "digest_applied_flag_true" for a in failed)


def test_check_turn_2_digest_verifies_raw_file_exists(tmp_path):
    engine = _engine()
    raw_body = "x" * 5000
    pico, raw_file = _pico_stub_with_digested_message(raw_body, tmp_path / "runs" / "tool_results")
    raw_file.unlink()  # remove the raw file → check should fail
    asserts = engine.check_turn_2_digest(_turn_2_result_stub(), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "raw_file_exists_on_disk" for a in failed)


def _turn_3_result_stub(**overrides):
    defaults = dict(
        turn=3,
        user_prompt="再看一下",
        expected_behavior="injection_dropped",
        final_answer="ok",
        metadata={
            "injection_budget": 500,
            "injection_dropped": ["checkpoint", "project_structure"],
            "injection_tokens": {
                "workspace_state": 100,
                "memory_index": 50,
                "project_structure": 0,
                "recalled_memory": 200,
                "checkpoint": 0,
            },
        },
        session_message_count_before=6,
        session_message_count_after=8,
        provider_call_count_this_turn=1,
        duration_ms=100,
        usage={},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=8,
        current_user_content="",
        usage_complete=True,
        request_metadata_by_call=({},),
        system_prefix_hashes=("cache-key",),
        action_origins=("provider_text",),
        actual_user_contents=("prompt",),
        run_id="run-3",
        task_state_terminal=True,
        report_terminal=True,
        trace_terminal=True,
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def test_check_turn_3_injection_drop_passes_when_checkpoint_dropped():
    engine = _engine()
    asserts = engine.check_turn_3_injection_drop(_turn_3_result_stub())
    assert len(asserts) == 4
    assert all(a.passed for a in asserts), [a for a in asserts if not a.passed]


def test_check_turn_3_injection_drop_accepts_checkpoint_zero_tokens():
    """Assertion 14 accepts either dropped OR zero-tokens-so-never-rendered."""
    engine = _engine()
    result = _turn_3_result_stub(metadata={
        "injection_budget": 500,
        "injection_dropped": ["project_structure"],  # checkpoint NOT dropped
        "injection_tokens": {
            "workspace_state": 100, "memory_index": 50,
            "project_structure": 0, "recalled_memory": 200,
            "checkpoint": 0,  # zero tokens — never rendered — should still pass
        },
    })
    asserts = engine.check_turn_3_injection_drop(result)
    failed = [a for a in asserts if not a.passed]
    assert not any(a.name == "checkpoint_dropped_or_zero_tokens" for a in failed)


def test_check_turn_3_injection_drop_fails_when_recalled_memory_dropped():
    engine = _engine()
    result = _turn_3_result_stub(metadata={
        "injection_budget": 500,
        "injection_dropped": ["checkpoint", "project_structure", "recalled_memory"],
        "injection_tokens": {"recalled_memory": 0, "checkpoint": 0},
    })
    asserts = engine.check_turn_3_injection_drop(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "recalled_memory_not_dropped" for a in failed)


def _turn_4_result_stub(**overrides):
    defaults = dict(
        turn=4,
        user_prompt="总结",
        expected_behavior="history_dropped",
        final_answer="ok",
        metadata={
            "dropped_messages": 4,
            "messages_tokens": 1000,
        },
        session_message_count_before=14,
        session_message_count_after=16,
        provider_call_count_this_turn=1,
        duration_ms=100,
        usage={},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=10,  # smaller than session (drop reached wire)
        current_user_content="",
        usage_complete=True,
        request_metadata_by_call=({},),
        system_prefix_hashes=("cache-key",),
        action_origins=("provider_text",),
        actual_user_contents=("prompt",),
        run_id="run-4",
        task_state_terminal=True,
        report_terminal=True,
        trace_terminal=True,
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def _pico_stub_with_history():
    """A pico session with 16 messages including one balanced tool_use pair."""
    pico = MagicMock()
    pico.session = {
        "messages": [
            {"role": "user", "content": "q1", "_pico_meta": {}},
            {"role": "assistant", "content": "a1", "_pico_meta": {}},
            {"role": "user", "content": "q2", "_pico_meta": {}},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read", "input": {}}], "_pico_meta": {}},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}], "_pico_meta": {}},
            {"role": "assistant", "content": "a2", "_pico_meta": {}},
        ] + [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}", "_pico_meta": {}} for i in range(10)]
    }
    return pico


def test_check_turn_4_history_drop_passes_when_all_invariants_hold():
    engine = _engine()
    pico = _pico_stub_with_history()
    asserts = engine.check_turn_4_history_drop(_turn_4_result_stub(), pico)
    assert len(asserts) == 5
    assert all(a.passed for a in asserts), [(a.name, a.actual) for a in asserts if not a.passed]


def test_check_turn_4_pairing_invariant_catches_orphan_tool_use():
    engine = _engine()
    pico = MagicMock()
    # orphan tool_use — no matching tool_result
    pico.session = {"messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "orphan_x", "name": "read", "input": {}}], "_pico_meta": {}},
    ]}
    asserts = engine.check_turn_4_history_drop(_turn_4_result_stub(), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "no_orphan_tool_use" for a in failed)


def test_check_turn_4_pairing_invariant_requires_immediate_tool_result():
    pico = MagicMock()
    pico.session = {
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tool-1", "name": "read", "input": {}}],
                "_pico_meta": {},
            },
            {"role": "assistant", "content": "intervening", "_pico_meta": {}},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "result"}],
                "_pico_meta": {},
            },
        ]
    }

    assertions = _engine().check_turn_4_history_drop(_turn_4_result_stub(), pico)

    assert any(
        assertion.name == "no_orphan_tool_use" and not assertion.passed
        for assertion in assertions
    )


def test_global_pairing_assertion_rejects_a_separated_tool_result(tmp_path):
    pico = _pico_stub_with_persisted_v3(tmp_path)
    pico.session["messages"] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tool-1", "name": "read", "input": {}}],
            "_pico_meta": {},
        },
        {"role": "assistant", "content": "intervening", "_pico_meta": {}},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "result"}],
            "_pico_meta": {},
        },
    ]
    pico.session_path.write_text(json.dumps(pico.session), encoding="utf-8")

    assertions = _engine().check_global(
        [_turn_result_stub(action_origins=("native_tool_use",))],
        pico,
    )

    assert any(
        assertion.name == "canonical_tool_pairs_immediately_match"
        and not assertion.passed
        for assertion in assertions
    )


def test_check_turn_4_fails_when_dropped_messages_zero():
    engine = _engine()
    pico = _pico_stub_with_history()
    asserts = engine.check_turn_4_history_drop(_turn_4_result_stub(metadata={"dropped_messages": 0, "messages_tokens": 500}), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "dropped_messages_gt_zero" for a in failed)


def _turn_1_result_stub_for_cache(cache_key="k"):
    return _turn_result_stub(
        metadata={
            "intent": {"name": "recall", "matched_keyword": "上次", "matched_reason": ""},
            "injection_tokens": {"recalled_memory": 10},
            "recall.error_count": 0,
            "system_prefix_hash": cache_key,
            "injection_budget": 500,
            "system_tokens": 100, "tools_tokens": 50,
            "messages_count": 2, "messages_tokens": 40, "injection_truncated": {},
            "injection_dropped": [], "recall.last_error": "",
            "dropped_messages": 0,
            "cache_control_breakpoints": [],
        },
        system_prefix_hashes=(cache_key,),
    )


def _turn_5_result_stub(system_prefix_hash="abc", **overrides):
    metadata = {
        "cache_control_breakpoints": [10],
        "system_prefix_hash": system_prefix_hash,
        "system_tokens": 100, "tools_tokens": 50, "messages_count": 12,
        "messages_tokens": 500, "injection_tokens": {}, "injection_truncated": {},
        "injection_dropped": [], "injection_budget": 500,
        "intent": {"name": "default", "matched_keyword": "", "matched_reason": ""},
        "recall.error_count": 0, "recall.last_error": "",
        "dropped_messages": 0,
    }
    defaults = dict(
        turn=5,
        user_prompt="done",
        expected_behavior="cache_anchor_verified",
        final_answer="ok",
        metadata=metadata,
        session_message_count_before=16, session_message_count_after=18,
        provider_call_count_this_turn=1, duration_ms=100,
        usage={"cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
        stopped_at_step_limit=False, error=None,
        provider_input_messages_len=12, current_user_content="",
        usage_complete=True,
        request_metadata_by_call=({},),
        system_prefix_hashes=(system_prefix_hash,),
        action_origins=("provider_text",),
        actual_user_contents=("prompt",),
        run_id="run-5",
        task_state_terminal=True,
        report_terminal=True,
        trace_terminal=True,
    )
    defaults.update(overrides)
    return TurnResult(**defaults)


def test_check_turn_5_cache_anchor_passes_when_cache_key_stable():
    engine = _engine()
    all_results = [
        _turn_1_result_stub_for_cache(cache_key="k"),
        _turn_1_result_stub_for_cache(cache_key="k"),
        _turn_1_result_stub_for_cache(cache_key="k"),
        _turn_1_result_stub_for_cache(cache_key="k"),
        _turn_5_result_stub(system_prefix_hash="k"),
    ]
    asserts = engine.check_turn_5_cache_anchor(all_results[-1], all_results)
    assert len(asserts) == 5
    assert all(a.passed for a in asserts), [(a.name, a.actual) for a in asserts if not a.passed]


def test_check_turn_5_fails_when_cache_key_drifts():
    engine = _engine()
    all_results = [
        _turn_1_result_stub_for_cache(cache_key="k1"),
        _turn_1_result_stub_for_cache(cache_key="k2"),  # drift!
    ]
    all_results.append(_turn_5_result_stub(system_prefix_hash="k1"))
    asserts = engine.check_turn_5_cache_anchor(all_results[-1], all_results)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "system_prefix_hash_stable_across_turns" for a in failed)


def test_deepseek_cache_assertions_do_not_require_cache_tokens():
    engine = _engine(provider="deepseek")
    all_results = [
        _turn_1_result_stub_for_cache(cache_key="stable"),
        _turn_1_result_stub_for_cache(cache_key="stable"),
        _turn_5_result_stub(
            system_prefix_hash="stable",
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
    ]

    assertions = engine.check_turn_5_cache_anchor(all_results[-1], all_results)

    assert all(assertion.passed for assertion in assertions), assertions


def test_check_global_passes_under_budget(tmp_path):
    engine = _engine()
    all_results = [
        _turn_result_stub(usage={"input_tokens": 1000, "output_tokens": 200}, provider_call_count_this_turn=1),
        _turn_result_stub(
            turn=2,
            usage={"input_tokens": 1500, "output_tokens": 300},
            provider_call_count_this_turn=2,
            system_prefix_hashes=("cache-key", "cache-key"),
            action_origins=("native_tool_use",),
        ),
        _turn_result_stub(turn=3, usage={"input_tokens": 1200, "output_tokens": 250}, provider_call_count_this_turn=1),
    ]
    asserts = engine.check_global(all_results, _pico_stub_with_persisted_v3(tmp_path))
    assert all(a.passed for a in asserts)


def test_check_global_fails_when_provider_calls_exceeded():
    engine = _engine()
    all_results = [
        _turn_result_stub(provider_call_count_this_turn=8),
        _turn_result_stub(turn=2, provider_call_count_this_turn=8),  # sum = 16 > 15
    ]
    asserts = engine.check_global(all_results, MagicMock())
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "total_provider_calls_under_cap" for a in failed)


def test_check_global_uses_nondefault_provider_call_cap():
    assertions = _engine(max_provider_calls=1).check_global(
        [
            _turn_result_stub(provider_call_count_this_turn=1),
            _turn_result_stub(turn=2, provider_call_count_this_turn=1),
        ],
        MagicMock(),
    )

    assert any(
        assertion.name == "total_provider_calls_under_cap" and not assertion.passed
        for assertion in assertions
    )


def test_check_global_fails_when_tokens_exceeded():
    engine = _engine()
    all_results = [
        _turn_result_stub(usage={"input_tokens": 150000, "output_tokens": 60000}),
    ]
    asserts = engine.check_global(all_results, MagicMock())
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "total_tokens_under_cap" for a in failed)


def _passing_assertion(name="pass"):
    return Assertion(name=name, passed=True, expected="true", actual="true")


def test_report_cannot_pass_when_aborted_or_short(tmp_path):
    reporter = Reporter(_config(), tmp_path)

    report_path = reporter.write_json(
        all_results=[],
        all_assertions={},
        config=reporter.config,
        totals={},
        wall_time_ms=1,
        aborted_reason="provider_error_turn_1",
        expected_turn_count=5,
        session_schema=3,
        git_head="abc",
    )

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["overall_pass"] is False
    assert payload["aborted_reason"] == "provider_error_turn_1"


def test_report_cannot_pass_with_an_empty_turn_assertion_list(tmp_path):
    reporter = Reporter(_config(), tmp_path)
    results = [_turn_result_stub(), _turn_result_stub(turn=2)]

    report_path = reporter.write_json(
        results,
        {1: [_passing_assertion()], 2: [], "global": [_passing_assertion()]},
        reporter.config,
        {},
        1,
        aborted_reason=None,
        expected_turn_count=2,
        session_schema=3,
        git_head="abc",
    )

    assert json.loads(report_path.read_text(encoding="utf-8"))["overall_pass"] is False


def test_report_cannot_pass_with_only_global_assertions(tmp_path):
    reporter = Reporter(_config(), tmp_path)

    report_path = reporter.write_json(
        [_turn_result_stub()],
        {"global": [_passing_assertion()]},
        reporter.config,
        {},
        1,
        aborted_reason=None,
        expected_turn_count=1,
        session_schema=3,
        git_head="abc",
    )

    assert json.loads(report_path.read_text(encoding="utf-8"))["overall_pass"] is False


def test_report_does_not_serialize_provider_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PICO_DEEPSEEK_API_KEY", "sentinel-secret")
    reporter = Reporter(_config(), tmp_path)

    report_path = reporter.write_json(
        [_turn_result_stub()],
        {1: [_passing_assertion()], "global": [_passing_assertion()]},
        reporter.config,
        {},
        1,
        aborted_reason=None,
        expected_turn_count=1,
        session_schema=3,
        git_head="abc",
    )

    assert "sentinel-secret" not in report_path.read_text(encoding="utf-8")


def _security_assertions(artifact_security, calls):
    pico = MagicMock()
    pico.model_client.calls = calls
    assertions = _engine().check_global(
        [_turn_result_stub(action_origins=("native_tool_use",))],
        pico,
        artifact_security,
    )
    return {
        assertion.name: assertion.passed
        for assertion in assertions
        if assertion.name
        in {
            "provider_payloads_exclude_api_key",
            "active_artifacts_exclude_api_key",
            "active_private_artifact_modes",
        }
    }


def test_global_security_assertions_fail_independently():
    clean = {"files_scanned": 3, "secret_hits": [], "mode_failures": []}
    assert _security_assertions(
        clean,
        [{"payload_secret_clean": False}],
    ) == {
        "provider_payloads_exclude_api_key": False,
        "active_artifacts_exclude_api_key": True,
        "active_private_artifact_modes": True,
    }
    assert _security_assertions(
        {**clean, "secret_hits": [".pico/runs/run/trace.jsonl"]},
        [{"payload_secret_clean": True}],
    ) == {
        "provider_payloads_exclude_api_key": True,
        "active_artifacts_exclude_api_key": False,
        "active_private_artifact_modes": True,
    }
    assert _security_assertions(
        {**clean, "mode_failures": [".pico/runs/run/trace.jsonl:0644"]},
        [{"payload_secret_clean": True}],
    ) == {
        "provider_payloads_exclude_api_key": True,
        "active_artifacts_exclude_api_key": True,
        "active_private_artifact_modes": False,
    }
    assert all(
        _security_assertions(
            clean,
            [{"payload_secret_clean": True}],
        ).values()
    )


def test_report_redacts_full_payload_and_writes_safe_artifact_summary(tmp_path):
    from pico.security import redact_artifact

    secret = "ghp_" + "R" * 32
    reporter = Reporter(_config(), tmp_path)
    result = _turn_result_stub(user_prompt=secret, final_answer=secret)
    assertion = Assertion(
        name="safe",
        passed=False,
        expected=secret,
        actual=secret,
    )
    artifact_security = {
        "files_scanned": 2,
        "secret_hits": [],
        "mode_failures": [],
    }

    report_path = reporter.write_json(
        [result],
        {1: [assertion], "global": [_passing_assertion()]},
        reporter.config,
        {},
        1,
        aborted_reason=secret,
        expected_turn_count=1,
        session_schema=3,
        git_head="abc",
        artifact_security=artifact_security,
        redactor=lambda value: redact_artifact(
            value,
            env={"PICO_LIVE_API_KEY": secret},
        ),
        forbidden_values=(secret,),
    )

    text = report_path.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert secret not in text
    assert payload["artifact_security"] == artifact_security
    if os.name == "posix":
        assert report_path.stat().st_mode & 0o777 == 0o600


def test_provider_wrapper_blocks_payload_leak_before_delegate():
    secret = "ghp_" + "W" * 32
    delegate = MagicMock()
    wrapper = run_live_session._SniffingProviderWrapper(
        delegate,
        forbidden_values=(secret,),
    )

    with pytest.raises(run_live_session.SensitiveDataBlockedError):
        wrapper.complete(
            system="safe",
            tools=[],
            messages=[{"role": "user", "content": secret}],
            max_tokens=10,
        )

    delegate.complete.assert_not_called()
    assert wrapper.calls == [
        {
            "last_user_content": secret,
            "call_ts_ns": wrapper.calls[0]["call_ts_ns"],
            "payload_secret_clean": False,
        }
    ]


def test_main_preflight_failure_never_constructs_provider(tmp_path, monkeypatch):
    make_client = MagicMock()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_live_session, "parse_args", lambda **_kwargs: _config())
    monkeypatch.setattr(
        run_live_session,
        "check_env",
        MagicMock(side_effect=SystemExit(2)),
    )
    monkeypatch.setattr(run_live_session, "make_live_client", make_client)

    with pytest.raises(SystemExit, match="2"):
        run_live_session.main()

    make_client.assert_not_called()
