"""Offline tests for the live-e2e trace and assertion harness.

These tests never enter the normal ``main`` path or create a provider client.
"""

import hashlib
import json
import os
import sys
import tomllib
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
from pico.agent.model_capabilities import (
    TokenAccounting,
    build_model_budget,
    resolve_model_capabilities,
)


def _config(**overrides):
    defaults = dict(
        repo_root=Path.cwd(),
        provider="anthropic",
        model="test-model",
        max_model_attempts=15,
        max_total_tokens=200_000,
        request_timeout_seconds=300,
        max_wall_seconds=900,
        reset=False,
        verbose=False,
    )
    defaults.update(overrides)
    return RunConfig(**defaults)


def _engine(**overrides):
    return AssertionEngine(_config(**overrides))


def _settings(**overrides):
    defaults = {
        "provider": "anthropic",
        "model": "test-model",
        "base_url": "https://api.anthropic.com/v1",
        "api_key": "test-key",
        "api_key_env": "PICO_API_KEY",
        "transport": "anthropic_messages",
        "auth_mode": "x-api-key",
        "capabilities": {},
    }
    defaults.update(overrides)
    return defaults


def test_live_fixture_uses_model_budget_and_compaction_contract():
    fixture = tomllib.loads(run_live_session.FIXTURE_PICO_TOML)
    model = fixture["model"]
    context = fixture["context"]
    compaction = context["compaction"]
    capabilities = resolve_model_capabilities(
        "live-fixture",
        model_config=model,
    )
    budget = build_model_budget(
        capabilities,
        reserve_tokens=compaction["reserve_tokens"],
        keep_recent_tokens=compaction["keep_recent_tokens"],
        system_tools_hard_cap=context["system_tools_hard_cap"],
        source_pool_tokens=context["source_pool_tokens"],
    )

    assert budget.output_tokens == 4096
    assert budget.reserve_tokens == 4096
    assert budget.input_limit == 20480
    assert budget.keep_recent_tokens == 4096
    assert budget.system_tools_hard_cap == 4915
    assert budget.source_pool_tokens == 3072
    assert context["tool_results"] == {
        "inline_tokens": 4096,
        "digest_tokens": 512,
    }


def test_compaction_fixture_appends_valid_inert_canonical_history():
    store = MagicMock()
    pico = SimpleNamespace(
        session={"id": "live-session", "messages": []},
        session_store=store,
    )

    count = run_live_session.seed_compaction_fixture(pico)

    assert count == run_live_session.COMPACTION_FIXTURE_MESSAGES
    assert len(pico.session["messages"]) == count
    validate_messages = run_live_session.validate_messages
    validate_messages(pico.session["messages"], require_meta=True)
    store.append_messages.assert_called_once_with(
        "live-session",
        pico.session["messages"],
    )
    assert all(
        message["_pico_meta"]["origin"] == "live_e2e_compaction_fixture"
        for message in pico.session["messages"]
    )
    accounting = TokenAccounting()
    fixture_tokens = sum(
        accounting.count_message(message) for message in pico.session["messages"]
    )
    fixture_config = tomllib.loads(run_live_session.FIXTURE_PICO_TOML)
    assert fixture_tokens > (
        fixture_config["model"]["context_window"]
        - fixture_config["context"]["compaction"]["reserve_tokens"]
    )


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
        assert result["mode_failures"] == [".pico/runs/run-test/trace.jsonl:0644"]


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


def test_fixture_missing_backup_never_deletes_existing_config(tmp_path):
    original = b"ordinary = true\n"
    config = tmp_path / "pico.toml"
    config.write_bytes(original)
    seed = tmp_path / "seed.md"
    seed.write_text("safe seed\n", encoding="utf-8")
    fixture = run_live_session.FixtureManager(tmp_path)
    fixture._seed_source = seed
    fixture.__enter__()
    (tmp_path / run_live_session.BACKUP_REL).unlink()

    fixture.__exit__(None, None, None)

    assert config.exists()
    assert fixture.cleanup_errors == ["config_backup_missing"]
    assert fixture.restoration_status()["restored"] is False


def test_fixture_enter_failure_restores_original_config(tmp_path):
    original = b"ordinary = true\n"
    config = tmp_path / "pico.toml"
    config.write_bytes(original)
    fixture = run_live_session.FixtureManager(tmp_path)
    fixture._seed_source = tmp_path / "missing-seed.md"

    with pytest.raises(FileNotFoundError):
        fixture.__enter__()

    assert config.read_bytes() == original
    assert not (tmp_path / run_live_session.BACKUP_REL).exists()


def test_parse_args_uses_repo_env_and_rejects_provider_override(tmp_path):
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=openai\n"
        "PICO_MODEL=gpt-test\n"
        "PICO_API_URL=https://api.openai.com/v1\n"
        "PICO_API_KEY=test-key\n"
        "PICO_API_VARIANT=responses\n"
        "PICO_AUTH_MODE=auto\n",
        encoding="utf-8",
    )

    config = run_live_session.parse_args(
        ["--repo-root", str(tmp_path)],
        process_env={},
    )

    assert config.provider == "openai"
    assert config.model == "gpt-test"
    with pytest.raises(SystemExit) as caught:
        run_live_session.parse_args(["--provider", "openai"])
    assert caught.value.code == 2


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_project_env_uses_canonical_selected_provider_settings(tmp_path, provider):
    base_url = f"https://{provider}.example/v1"
    lines = [
        f"PICO_PROVIDER={provider}",
        f"PICO_MODEL={provider}-test-model",
        f"PICO_API_URL={base_url}",
        f"PICO_API_KEY=sentinel-{provider}",
        "PICO_API_VARIANT=auto",
        "PICO_AUTH_MODE=auto",
    ]
    (tmp_path / ".env").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    with patch.dict(os.environ, {}, clear=True):
        settings = run_live_session.provider_settings(
            tmp_path,
            project_env=run_live_session.read_project_env(tmp_path),
            process_env={},
        )

    expected_auth_mode = "x-api-key" if provider == "anthropic" else "bearer"
    assert settings["api_key"] == f"sentinel-{provider}"
    assert settings["model"] == (f"{provider}-test-model")
    assert settings["base_url"] == base_url
    assert settings["auth_mode"] == expected_auth_mode
    assert settings["capabilities"].get("prompt_cache", False) is (
        provider == "anthropic"
    )


def test_project_env_uses_canonical_ollama_settings(tmp_path):
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=ollama\n"
        "PICO_MODEL=ollama-test-model\n"
        "PICO_API_URL=http://127.0.0.1:11435\n"
        "PICO_API_KEY=\n"
        "PICO_API_VARIANT=auto\n"
        "PICO_AUTH_MODE=auto\n",
        encoding="utf-8",
    )

    settings = run_live_session.provider_settings(
        tmp_path,
        project_env=run_live_session.read_project_env(tmp_path),
        process_env={},
    )

    assert settings == {
        "provider": "ollama",
        "api_key": "",
        "api_key_env": "PICO_API_KEY",
        "model": "ollama-test-model",
        "base_url": "http://127.0.0.1:11435",
        "transport": "ollama_chat",
        "auth_mode": "none",
        "capabilities": {},
    }


def test_openai_live_client_uses_native_responses_adapter():
    from pico.providers.openai_responses import OpenAIResponsesModelClient

    client = run_live_session.make_live_client(
        _config(provider="openai", request_timeout_seconds=321),
        settings={
            "api_key": "sentinel-openai",
            "model": "test-model",
            "base_url": "https://openai.example.invalid/v1",
            "transport": "openai_responses",
            "auth_mode": "bearer",
            "capabilities": {},
        },
    )

    assert isinstance(client._inner, OpenAIResponsesModelClient)
    assert client._inner.timeout == 321


def test_ollama_live_client_uses_native_chat_adapter():
    from pico.providers.ollama_chat import OllamaChatModelClient

    client = run_live_session.make_live_client(
        _config(provider="ollama", request_timeout_seconds=321),
        settings={
            "api_key": "",
            "model": "test-model",
            "base_url": "http://127.0.0.1:11434",
            "transport": "ollama_chat",
            "auth_mode": "none",
            "capabilities": {},
        },
    )

    assert isinstance(client._inner, OllamaChatModelClient)
    assert client._inner.timeout == 321


def test_ollama_live_preflight_does_not_require_api_key():
    run_live_session.check_env(
        _config(provider="ollama"),
        settings={"api_key": "", "model": "test-model", "base_url": "local"},
    )


def test_main_reset_uses_repo_root_without_reading_provider_env(tmp_path, monkeypatch):
    events = []
    monkeypatch.chdir(tmp_path)
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
    assert events == [("parse", None), ("reset", tmp_path)]


def test_main_constructs_live_pico_with_only_read_file(tmp_path, monkeypatch):
    import pico.runtime.application
    import pico.state.session_store
    import pico.workspace.context

    captured = {}

    def capture_pico(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("construction captured")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_live_session, "parse_args", lambda **_kwargs: _config())
    monkeypatch.setattr(
        run_live_session, "provider_settings", lambda *_args, **_kwargs: _settings()
    )
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
    monkeypatch.setattr(
        run_live_session, "make_live_client", lambda _config, **_kwargs: object()
    )
    monkeypatch.setattr(
        pico.workspace.context.WorkspaceContext,
        "build",
        lambda _root: object(),
    )
    monkeypatch.setattr(
        pico.state.session_store, "SessionStore", lambda _root: object()
    )
    monkeypatch.setattr(pico.runtime.application, "Pico", capture_pico)

    assert run_live_session.main() == 4
    assert captured["options"].allowed_tools == ("read_file",)
    assert captured["options"].max_steps == 2


def test_read_turn_trace_aggregates_every_model_turn(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                {"event": "model_requested", "attempt_origin": "initial"},
                {
                    "event": "model_turn",
                    "request_metadata": {
                        "system_prefix_hash": "k",
                        "messages_count": 1,
                    },
                    "completion_usage": {"input_tokens": 10, "output_tokens": 2},
                    "transport_attempts": 1,
                    "transport_retries": 0,
                    "transport_evidence_complete": True,
                },
                {"event": "model_requested", "attempt_origin": "tool_followup"},
                {
                    "event": "action_decoded",
                    "action_type": "tool",
                    "origin": "native_tool_use",
                },
                {
                    "event": "model_turn",
                    "request_metadata": {
                        "system_prefix_hash": "k",
                        "messages_count": 3,
                    },
                    "completion_usage": {
                        "input_tokens": 20,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 8,
                    },
                    "transport_attempts": 1,
                    "transport_retries": 0,
                    "transport_evidence_complete": True,
                },
                {
                    "event": "action_decoded",
                    "action_type": "final",
                    "origin": "text_protocol",
                },
            ]
        ),
        encoding="utf-8",
    )

    captured = run_live_session.read_turn_trace(trace)

    assert captured["model_turns"] == 2
    assert captured["model_attempts"] == 2
    assert captured["model_failures"] == 0
    assert captured["transport_attempts"] == 2
    assert captured["transport_retries"] == 0
    assert captured["transport_evidence_complete"] is True
    assert captured["billing_ambiguous"] is False
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


def test_auxiliary_compaction_call_is_counted_without_inflating_agent_turns():
    captured = {
        "model_turns": 1,
        "model_attempts": 1,
        "model_failures": 0,
        "transport_attempts": 1,
        "transport_retries": 0,
        "transport_evidence_complete": True,
        "billing_ambiguous": False,
        "usage": {key: 0 for key in run_live_session._LIVE_USAGE_KEYS},
        "usage_complete": True,
        "request_metadata": [{}],
        "system_prefix_hashes": ["key"],
        "action_origins": [],
    }
    captured["usage"].update({"input_tokens": 100, "output_tokens": 10})
    summary_call = {
        "call_kind": "session_summary",
        "completed": True,
        "usage": {"input_tokens": 50, "output_tokens": 5},
        "transport_attempts": 1,
        "transport_retries": 0,
    }

    merged = run_live_session._merge_auxiliary_call_evidence(
        captured,
        [summary_call],
    )

    assert merged["model_turns"] == 1
    assert merged["model_attempts"] == 2
    assert merged["model_failures"] == 0
    assert merged["usage"]["input_tokens"] == 150
    assert merged["usage"]["output_tokens"] == 15
    assert merged["transport_attempts"] == 2
    assert merged["usage_complete"] is True
    assert merged["billing_ambiguous"] is False


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

    assert captured["model_turns"] == 0
    assert captured["usage_complete"] is False


def test_read_turn_trace_marks_non_utf8_artifact_usage_unknown(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_bytes(b"\xff")

    captured = run_live_session.read_turn_trace(trace)

    assert captured["model_turns"] == 0
    assert captured["usage_complete"] is False


def test_read_run_terminal_status_uses_each_persisted_artifact(tmp_path):
    from pico.state.run_store import RunStore

    run_store = RunStore(tmp_path)
    task_state = SimpleNamespace(run_id="run-1")
    run_store.task_state_path(task_state).parent.mkdir(parents=True)
    run_store.task_state_path(task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": "final_answer_returned"}),
        encoding="utf-8",
    )
    run_store.report_path(task_state).write_text(
        json.dumps({"run": {"status": "stopped", "stop_reason": "step_limit_reached"}}),
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
        json.dumps({"run": {"status": "failed", "stop_reason": ""}}),
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
    from pico.state.run_store import RunStore

    run_store = RunStore(tmp_path)
    task_state = SimpleNamespace(run_id="run-invalid-reason")
    run_store.task_state_path(task_state).parent.mkdir(parents=True)
    run_store.task_state_path(task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": "done"}),
        encoding="utf-8",
    )
    run_store.report_path(task_state).write_text(
        json.dumps({"run": {"status": "completed", "stop_reason": stop_reason}}),
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
    from pico.state.run_store import RunStore

    run_store = RunStore(tmp_path)
    task_state = SimpleNamespace(run_id="run-one-bad-artifact")
    run_store.task_state_path(task_state).parent.mkdir(parents=True)
    run_store.task_state_path(task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": "done"}),
        encoding="utf-8",
    )
    run_store.report_path(task_state).write_text(
        json.dumps({"run": {"status": "completed", "stop_reason": "done"}}),
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
    from pico.state.run_store import RunStore

    run_store = RunStore(tmp_path)
    previous_task_state = SimpleNamespace(run_id="previous-run")
    run_store.task_state_path(previous_task_state).parent.mkdir(parents=True)
    run_store.task_state_path(previous_task_state).write_text(
        json.dumps({"status": "completed", "stop_reason": "done"}),
        encoding="utf-8",
    )
    run_store.report_path(previous_task_state).write_text(
        json.dumps({"run": {"status": "completed", "stop_reason": "done"}}),
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
    assert result.model_turns_this_turn == 0
    assert result.usage_complete is False
    assert result.metadata == {}
    assert result.actual_user_contents == ()
    assert result.run_id == ""
    assert not result.task_state_terminal
    assert not result.report_terminal
    assert not result.trace_terminal


def test_turn_runner_uses_first_trace_call_as_current_turn_evidence(tmp_path):
    from pico.state.run_store import RunStore

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
        json.dumps({"run": {"status": "completed", "stop_reason": "done"}}),
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
                        "action_type": "tool",
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


def _pico_stub_with_persisted_tree(tmp_path):
    del tmp_path
    session = {
        "record_type": "session",
        "format_version": run_live_session.SESSION_FORMAT_VERSION,
        "id": "live-test-session",
        "messages": _canonical_session_messages(),
    }
    pico = SimpleNamespace(
        session=session,
        model_client=SimpleNamespace(calls=[{"payload_secret_clean": True}]),
    )
    pico.session_store = SimpleNamespace(
        load_tree=lambda _session_id: SimpleNamespace(
            header={
                "record_type": run_live_session.SESSION_HEADER_RECORD_TYPE,
                "format_version": run_live_session.SESSION_FORMAT_VERSION,
            },
            projection=pico.session,
        )
    )
    return pico


def _turn_result_stub(**overrides):
    defaults = dict(
        turn=1,
        user_prompt="上次讨论过 cache invariant 的问题",
        expected_behavior="recall_triggered",
        final_answer="ok",
        metadata={
            "context_source_allocator": {"name": "priority_allocator"},
            "injection_tokens": {"recalled_memory": 42, "workspace_state": 10},
            "recall.error_count": 0,
        },
        session_message_count_before=0,
        session_message_count_after=2,
        model_turns_this_turn=1,
        model_attempts_this_turn=1,
        model_failures_this_turn=0,
        transport_attempts_this_turn=1,
        transport_retries_this_turn=0,
        transport_evidence_complete=True,
        billing_ambiguous=False,
        duration_ms=100,
        usage={"input_tokens": 10, "output_tokens": 5},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=1,
        current_user_content=(
            '<system-reminder><pico:recalled_memory path="workspace/notes/cache-invariant.md">'
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


def test_check_turn_1_recall_fails_when_priority_allocator_is_missing():
    engine = _engine()
    result = _turn_result_stub(
        metadata={
            "context_source_allocator": {"name": "unknown"},
            "injection_tokens": {"recalled_memory": 42},
            "recall.error_count": 0,
        }
    )
    asserts = engine.check_turn_1_recall(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "priority_allocator_active" for a in failed)


def test_check_turn_1_recall_fails_when_no_recall_block_rendered():
    engine = _engine()
    result = _turn_result_stub(
        current_user_content="上次讨论过什么",
        metadata={
            "context_source_allocator": {"name": "priority_allocator"},
            "injection_tokens": {"recalled_memory": 0},
            "recall.error_count": 0,
        },
    )
    asserts = engine.check_turn_1_recall(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "recalled_memory_block_present" for a in failed)


def test_check_turn_1_recall_fails_when_recall_error_nonzero():
    engine = _engine()
    result = _turn_result_stub(
        metadata={
            "context_source_allocator": {"name": "priority_allocator"},
            "injection_tokens": {"recalled_memory": 42},
            "recall.error_count": 3,
        }
    )
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
        model_turns_this_turn=2,
        model_attempts_this_turn=2,
        model_failures_this_turn=0,
        transport_attempts_this_turn=2,
        transport_retries_this_turn=0,
        transport_evidence_complete=True,
        billing_ambiguous=False,
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


def _pico_stub_with_digested_message(
    raw_body: str,
    run_dir: Path,
    source_hash: str | None = None,
):
    """Build a MagicMock pico whose session has a digested tool_result at the tail."""
    content_sha256 = hashlib.sha256(raw_body.encode("utf-8")).hexdigest()
    source_hash = source_hash or content_sha256[:16]
    raw_dir = run_dir / "tool_results"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / f"{source_hash}.txt"
    raw_file.write_text(raw_body, encoding="utf-8")

    pico = MagicMock()
    pico.run_store.run_dir.return_value = run_dir
    pico.session = {
        "messages": [
            {"role": "user", "content": "read"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "read_file",
                        "input": {"path": "x"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": (
                            "[digest] runtime.py (900 lines)\n- import\n"
                            f"[reference] content_sha256: sha256:{content_sha256} "
                            f"raw_result_id: tool_result:{source_hash}"
                        ),
                    }
                ],
                "_pico_meta": {
                    "digest_applied": True,
                    "source_hash": source_hash,
                    "tool_use_id": "t1",
                },
            },
        ]
    }
    return pico, raw_file


def test_check_turn_2_digest_passes_on_valid_state(tmp_path):
    engine = _engine()
    raw_body = "x" * 5000
    pico, raw_file = _pico_stub_with_digested_message(raw_body, tmp_path / "runs")
    result = _turn_2_result_stub()
    asserts = engine.check_turn_2_digest(result, pico)
    assert len(asserts) == 14
    assert all(a.passed for a in asserts), [
        (a.name, a.actual) for a in asserts if not a.passed
    ]


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_text_provider_turn_2_accepts_text_protocol_action(tmp_path, provider):
    pico, _ = _pico_stub_with_digested_message(
        "x" * 5000,
        tmp_path / "runs",
    )
    assertions = _engine(provider=provider).check_turn_2_digest(
        _turn_2_result_stub(action_origins=("text_protocol",)),
        pico,
    )

    action_assertion = next(
        assertion
        for assertion in assertions
        if assertion.name == "provider_tool_action_observed"
    )
    assert action_assertion.passed
    assert action_assertion.expected == "text_protocol in action_origins"


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
        if assertion.name == "injected_user_prompt_reaches_every_model_turn"
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
        if assertion.name == "injected_user_prompt_reaches_every_model_turn"
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
        "provider_tool_action_observed",
        "turn_usage_complete",
        "injected_user_prompt_reaches_every_model_turn",
        "system_prefix_hashes_cover_every_model_turn",
    } <= failed


def test_check_turn_2_digest_fails_when_no_digest_applied(tmp_path):
    engine = _engine()
    pico = MagicMock()
    pico.session = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "raw output",
                    }
                ],
                "_pico_meta": {"digest_applied": False, "tool_use_id": "t1"},
            },
        ]
    }
    asserts = engine.check_turn_2_digest(_turn_2_result_stub(), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "digest_applied_flag_true" for a in failed)


def test_check_turn_2_digest_verifies_raw_file_exists(tmp_path):
    engine = _engine()
    raw_body = "x" * 5000
    pico, raw_file = _pico_stub_with_digested_message(raw_body, tmp_path / "runs")
    raw_file.unlink()  # remove the raw file → check should fail
    asserts = engine.check_turn_2_digest(_turn_2_result_stub(), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "raw_file_exists_on_disk" for a in failed)


def test_check_turn_2_digest_rejects_model_visible_host_path(tmp_path):
    pico, raw_file = _pico_stub_with_digested_message("x" * 5000, tmp_path / "runs")
    tool_result = pico.session["messages"][-1]["content"][0]
    tool_result["content"] += f"\n(raw at {raw_file})"

    assertions = _engine().check_turn_2_digest(_turn_2_result_stub(), pico)

    host_path_assertion = next(
        assertion
        for assertion in assertions
        if assertion.name == "tool_result_content_hides_host_artifact_path"
    )
    assert not host_path_assertion.passed


def _turn_3_result_stub(**overrides):
    defaults = dict(
        turn=3,
        user_prompt="再看一下",
        expected_behavior="source_pool_bounded",
        final_answer="ok",
        metadata={
            "injection_budget": 500,
            "injection_dropped": ["project_structure"],
            "injection_truncated": {},
            "context_source_allocator": {
                "name": "priority_allocator",
                "pool_tokens": 500,
                "used_tokens": 450,
                "source_tokens": {
                    "workspace_state": 100,
                    "memory_index": 50,
                    "recalled_memory": 300,
                },
            },
            "context_breakdown": {
                "sources": [
                    {"name": "workspace_state", "hard_cap": 3072},
                    {"name": "memory_index", "hard_cap": 1024},
                    {"name": "recalled_memory", "hard_cap": 6144},
                ]
            },
        },
        session_message_count_before=6,
        session_message_count_after=8,
        model_turns_this_turn=1,
        model_attempts_this_turn=1,
        model_failures_this_turn=0,
        transport_attempts_this_turn=1,
        transport_retries_this_turn=0,
        transport_evidence_complete=True,
        billing_ambiguous=False,
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


def test_check_turn_3_source_allocator_passes_when_contract_holds():
    engine = _engine()
    asserts = engine.check_turn_3_source_allocator(_turn_3_result_stub())
    assert len(asserts) == 5
    assert all(a.passed for a in asserts), [a for a in asserts if not a.passed]


def test_check_turn_3_source_allocator_rejects_global_pool_overflow():
    engine = _engine()
    result = _turn_3_result_stub(
        metadata={
            "context_source_allocator": {
                "name": "priority_allocator",
                "pool_tokens": 500,
                "used_tokens": 501,
                "source_tokens": {"workspace_state": 501},
            },
            "context_breakdown": {
                "sources": [{"name": "workspace_state", "hard_cap": 3072}]
            },
            "injection_truncated": {},
        }
    )
    asserts = engine.check_turn_3_source_allocator(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "source_pool_not_exceeded" for a in failed)


def test_check_turn_3_source_allocator_rejects_partial_or_over_cap_source():
    engine = _engine()
    result = _turn_3_result_stub(
        metadata={
            "context_source_allocator": {
                "name": "priority_allocator",
                "pool_tokens": 1000,
                "used_tokens": 600,
                "source_tokens": {"memory_index": 600},
            },
            "context_breakdown": {
                "sources": [{"name": "memory_index", "hard_cap": 512}]
            },
            "injection_truncated": {"memory_index": True},
        }
    )
    asserts = engine.check_turn_3_source_allocator(result)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "whole_chunks_respect_source_caps" for a in failed)


def _turn_4_result_stub(**overrides):
    defaults = dict(
        turn=4,
        user_prompt="总结",
        expected_behavior="history_compacted",
        final_answer="ok",
        metadata={
            "dropped_messages": 0,
            "context_breakdown": {
                "compaction": {
                    "entry_id": "compact-1",
                    "summary_tokens": 800,
                    "reason": "budget_exceeded",
                    "compression_ratio": 0.25,
                }
            },
        },
        session_message_count_before=14,
        session_message_count_after=16,
        model_turns_this_turn=1,
        model_attempts_this_turn=1,
        model_failures_this_turn=0,
        transport_attempts_this_turn=1,
        transport_retries_this_turn=0,
        transport_evidence_complete=True,
        billing_ambiguous=False,
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
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "read", "input": {}}
                ],
                "_pico_meta": {},
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "r"}
                ],
                "_pico_meta": {},
            },
            {"role": "assistant", "content": "a2", "_pico_meta": {}},
        ]
        + [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"m{i}",
                "_pico_meta": {},
            }
            for i in range(10)
        ]
    }
    return pico


def test_check_turn_4_compaction_passes_when_all_invariants_hold():
    engine = _engine()
    pico = _pico_stub_with_history()
    asserts = engine.check_turn_4_compaction(_turn_4_result_stub(), pico)
    assert len(asserts) == 6
    assert all(a.passed for a in asserts), [
        (a.name, a.actual) for a in asserts if not a.passed
    ]


def test_check_turn_4_pairing_invariant_catches_orphan_tool_use():
    engine = _engine()
    pico = MagicMock()
    # orphan tool_use — no matching tool_result
    pico.session = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "orphan_x", "name": "read", "input": {}}
                ],
                "_pico_meta": {},
            },
        ]
    }
    asserts = engine.check_turn_4_compaction(_turn_4_result_stub(), pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "no_orphan_tool_use" for a in failed)


def test_check_turn_4_pairing_invariant_requires_immediate_tool_result():
    pico = MagicMock()
    pico.session = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tool-1", "name": "read", "input": {}}
                ],
                "_pico_meta": {},
            },
            {"role": "assistant", "content": "intervening", "_pico_meta": {}},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "result",
                    }
                ],
                "_pico_meta": {},
            },
        ]
    }

    assertions = _engine().check_turn_4_compaction(_turn_4_result_stub(), pico)

    assert any(
        assertion.name == "no_orphan_tool_use" and not assertion.passed
        for assertion in assertions
    )


def test_global_pairing_assertion_rejects_a_separated_tool_result(tmp_path):
    pico = _pico_stub_with_persisted_tree(tmp_path)
    pico.session["messages"] = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tool-1", "name": "read", "input": {}}
            ],
            "_pico_meta": {},
        },
        {"role": "assistant", "content": "intervening", "_pico_meta": {}},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "content": "result"}
            ],
            "_pico_meta": {},
        },
    ]

    assertions = _engine().check_global(
        [_turn_result_stub(action_origins=("native_tool_use",))],
        pico,
    )

    assert any(
        assertion.name == "canonical_tool_pairs_immediately_match"
        and not assertion.passed
        for assertion in assertions
    )


def test_check_turn_4_fails_when_messages_were_silently_dropped():
    engine = _engine()
    pico = _pico_stub_with_history()
    result = _turn_4_result_stub()
    result.metadata["dropped_messages"] = 4
    asserts = engine.check_turn_4_compaction(result, pico)
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "no_silent_history_drop" for a in failed)


def _turn_1_result_stub_for_cache(cache_key="k"):
    return _turn_result_stub(
        metadata={
            "context_source_allocator": {"name": "priority_allocator"},
            "injection_tokens": {"recalled_memory": 10},
            "recall.error_count": 0,
            "system_prefix_hash": cache_key,
            "injection_budget": 500,
            "system_tokens": 100,
            "tools_tokens": 50,
            "messages_count": 2,
            "messages_tokens": 40,
            "injection_truncated": {},
            "injection_dropped": [],
            "recall.last_error": "",
            "dropped_messages": 0,
            "cache_control_breakpoints": [],
        },
        system_prefix_hashes=(cache_key,),
    )


def _turn_5_result_stub(system_prefix_hash="abc", **overrides):
    metadata = {
        "cache_control_breakpoints": [10],
        "system_prefix_hash": system_prefix_hash,
        "system_tokens": 100,
        "tools_tokens": 50,
        "messages_count": 12,
        "messages_tokens": 500,
        "injection_tokens": {},
        "injection_truncated": {},
        "injection_dropped": [],
        "injection_budget": 500,
        "context_source_allocator": {"name": "priority_allocator"},
        "recall.error_count": 0,
        "recall.last_error": "",
        "dropped_messages": 0,
    }
    defaults = dict(
        turn=5,
        user_prompt="done",
        expected_behavior="cache_anchor_verified",
        final_answer="ok",
        metadata=metadata,
        session_message_count_before=16,
        session_message_count_after=18,
        model_turns_this_turn=1,
        model_attempts_this_turn=1,
        model_failures_this_turn=0,
        transport_attempts_this_turn=1,
        transport_retries_this_turn=0,
        transport_evidence_complete=True,
        billing_ambiguous=False,
        duration_ms=100,
        usage={"cache_read_input_tokens": 100, "cache_creation_input_tokens": 0},
        stopped_at_step_limit=False,
        error=None,
        provider_input_messages_len=12,
        current_user_content="",
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
    assert all(a.passed for a in asserts), [
        (a.name, a.actual) for a in asserts if not a.passed
    ]


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


def test_cache_assertions_do_not_require_provider_cache_counters():
    engine = _engine(provider="anthropic")
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
        _turn_result_stub(
            usage={"input_tokens": 1000, "output_tokens": 200}, model_turns_this_turn=1
        ),
        _turn_result_stub(
            turn=2,
            usage={"input_tokens": 1500, "output_tokens": 300},
            model_turns_this_turn=2,
            system_prefix_hashes=("cache-key", "cache-key"),
            action_origins=("native_tool_use",),
        ),
        _turn_result_stub(
            turn=3,
            usage={"input_tokens": 1200, "output_tokens": 250},
            model_turns_this_turn=1,
        ),
    ]
    asserts = engine.check_global(all_results, _pico_stub_with_persisted_tree(tmp_path))
    assert all(a.passed for a in asserts)


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_text_provider_global_accepts_text_protocol_action(tmp_path, provider):
    assertions = _engine(provider=provider).check_global(
        [_turn_result_stub(action_origins=("text_protocol",))],
        _pico_stub_with_persisted_tree(tmp_path),
    )

    action_assertion = next(
        assertion
        for assertion in assertions
        if assertion.name == "provider_tool_action_observed"
    )
    assert action_assertion.passed
    assert action_assertion.expected.endswith("text_protocol")


def test_check_global_fails_when_model_attempts_exceeded():
    engine = _engine()
    all_results = [
        _turn_result_stub(model_attempts_this_turn=8),
        _turn_result_stub(turn=2, model_attempts_this_turn=8),  # sum = 16 > 15
    ]
    asserts = engine.check_global(all_results, MagicMock())
    failed = [a for a in asserts if not a.passed]
    assert any(a.name == "total_model_attempts_under_cap" for a in failed)


def test_check_global_uses_nondefault_model_turn_cap():
    assertions = _engine(max_model_attempts=1).check_global(
        [
            _turn_result_stub(model_attempts_this_turn=1),
            _turn_result_stub(turn=2, model_attempts_this_turn=1),
        ],
        MagicMock(),
    )

    assert any(
        assertion.name == "total_model_attempts_under_cap" and not assertion.passed
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
    assert payload["record_type"] == "live_e2e_report"
    assert payload["format_version"] == 2
    assert payload["overall_pass"] is False
    assert payload["aborted_reason"] == "provider_error_turn_1"


@pytest.mark.parametrize("version", [None, True, 1.0, "2", 1])
def test_live_report_reader_rejects_noncurrent_header_before_business(
    tmp_path, version
):
    payload = {
        "record_type": "live_e2e_report",
        "format_version": version,
        "overall_pass": "poisoned-business-shape",
    }
    if version is None:
        payload.pop("format_version")
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="format_version"):
        run_live_session.load_live_report(path)


def test_live_report_reader_rejects_nested_duplicate_keys(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(
        '{"record_type":"live_e2e_report","format_version":1,'
        '"overall_pass":false,"totals":{"turns":1,"turns":2}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate"):
        run_live_session.load_live_report(path)


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
    monkeypatch.setenv("PICO_API_KEY", "sentinel-secret")
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
    from pico.security.redaction import redact_artifact

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
            env={"PICO_OPENAI_API_KEY": secret},
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
            "call_kind": "agent",
            "completed": False,
            "usage": {},
            "transport_attempts": None,
            "transport_retries": None,
        }
    ]


def test_provider_wrapper_marks_and_accounts_for_compaction_call():
    delegate = MagicMock()
    delegate.complete.return_value = SimpleNamespace(
        usage={"input_tokens": 120, "output_tokens": 12}
    )
    delegate.last_transport_attempts = 1
    wrapper = run_live_session._SniffingProviderWrapper(delegate)

    response = wrapper.complete(
        system=[{"type": "text", "text": "You compact coding-agent history"}],
        tools=[],
        messages=[{"role": "user", "content": "history"}],
        max_tokens=100,
    )

    assert response is delegate.complete.return_value
    assert wrapper.calls[0]["call_kind"] == "session_summary"
    assert wrapper.calls[0]["completed"] is True
    assert wrapper.calls[0]["usage"] == {
        "input_tokens": 120,
        "output_tokens": 12,
    }
    assert wrapper.calls[0]["transport_attempts"] == 1
    assert wrapper.calls[0]["transport_retries"] == 0


def test_main_preflight_failure_never_constructs_provider(tmp_path, monkeypatch):
    make_client = MagicMock()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_live_session, "parse_args", lambda **_kwargs: _config())
    monkeypatch.setattr(
        run_live_session, "provider_settings", lambda *_args, **_kwargs: _settings()
    )
    monkeypatch.setattr(
        run_live_session,
        "check_env",
        MagicMock(side_effect=SystemExit(2)),
    )
    monkeypatch.setattr(run_live_session, "make_live_client", make_client)

    with pytest.raises(SystemExit, match="2"):
        run_live_session.main()

    make_client.assert_not_called()


def test_v2_cli_rejects_removed_provider_call_and_mixed_timeout_flags(monkeypatch):
    for flag in ("--max-provider-calls", "--timeout-seconds"):
        monkeypatch.setattr(sys, "argv", ["run_live_session", flag, "1"])
        with pytest.raises(SystemExit, match="2"):
            run_live_session.parse_args()


@pytest.mark.parametrize(
    "flag",
    (
        "--max-model-attempts",
        "--max-total-tokens",
        "--request-timeout-seconds",
        "--max-wall-seconds",
    ),
)
def test_v2_cli_rejects_nonpositive_caps(monkeypatch, flag):
    monkeypatch.setattr(sys, "argv", ["run_live_session", flag, "0"])
    with pytest.raises(SystemExit, match="2"):
        run_live_session.parse_args()


def test_ollama_readiness_uses_bounded_model_probe(monkeypatch):
    monkeypatch.setattr(
        "pico.providers.probe.probe_model_client",
        lambda _client: {"status": "failed"},
    )

    assert (
        run_live_session.check_live_readiness(
            _config(provider="ollama"),
            settings={
                "api_key": "",
                "model": "test-model",
                "base_url": "http://127.0.0.1:11434",
                "transport": "ollama_chat",
                "auth_mode": "none",
                "capabilities": {},
            },
        )
        is False
    )


def _gate_assertions():
    return {
        1: [Assertion("behavior_ok", True, "", "", gate="behavior")],
        "global": [
            Assertion("transport_ok", True, "", "", gate="transport_cost"),
            Assertion("security_ok", True, "", "", gate="security"),
            Assertion("persistence_ok", True, "", "", gate="persistence"),
        ],
    }


def test_v2_gates_pass_only_with_complete_zero_retry_evidence(tmp_path):
    reporter = Reporter(_config(), tmp_path)
    result = _turn_result_stub()

    path = reporter.write_json(
        [result],
        _gate_assertions(),
        reporter.config,
        {"input_tokens": 10, "output_tokens": 5},
        10,
        aborted_reason=None,
        expected_turn_count=1,
        session_schema=1,
        git_head="abc",
    )
    payload = run_live_session.load_live_report(path)

    assert payload["overall_pass"] is True
    assert {name: gate["status"] for name, gate in payload["gates"].items()} == {
        "behavior": "pass",
        "transport_cost": "pass",
        "security": "pass",
        "persistence": "pass",
    }


def test_v2_transport_retry_is_degraded_and_evidence_gap_is_fail(tmp_path):
    reporter = Reporter(_config(), tmp_path)
    retry = _turn_result_stub(
        transport_attempts_this_turn=2,
        transport_retries_this_turn=1,
        billing_ambiguous=True,
    )
    retry_gates = reporter._build_gates([retry], _gate_assertions(), {}, 1)
    missing = _turn_result_stub(
        transport_attempts_this_turn=None,
        transport_retries_this_turn=None,
        transport_evidence_complete=False,
        billing_ambiguous=True,
    )
    missing_gates = reporter._build_gates([missing], _gate_assertions(), {}, 1)

    assert retry_gates["transport_cost"]["status"] == "degraded"
    assert missing_gates["transport_cost"]["status"] == "fail"


def test_v2_report_omits_prompt_answer_raw_assertion_and_exception(tmp_path):
    secret_text = "sensitive-prompt-and-answer"
    reporter = Reporter(_config(), tmp_path)
    result = _turn_result_stub(
        user_prompt=secret_text,
        final_answer=secret_text,
        error=f"RuntimeError: {secret_text}",
    )
    assertions = _gate_assertions()
    assertions[1][0] = Assertion(
        "behavior_ok", False, secret_text, secret_text, gate="behavior"
    )

    path = reporter.write_json(
        [result],
        assertions,
        reporter.config,
        {},
        1,
        aborted_reason="provider_error_turn_1",
        expected_turn_count=1,
        session_schema=1,
        git_head="abc",
    )
    text = path.read_text(encoding="utf-8")
    turn = json.loads(text)["turns"][0]

    assert secret_text not in text
    assert "user_prompt" not in turn
    assert "final_answer" not in turn
    assert turn["error_code"] == "turn_error"
    assert set(turn["assertions"][0]) == {"name", "gate", "passed"}


def test_fixture_restoration_is_verified_after_context_exit(tmp_path):
    original = b"ordinary = true\n"
    (tmp_path / "pico.toml").write_bytes(original)
    seed = tmp_path / "seed.md"
    seed.write_text("safe seed\n", encoding="utf-8")
    fixture = run_live_session.FixtureManager(tmp_path)
    fixture._seed_source = seed

    with fixture:
        assert fixture.restoration_status()["restored"] is False
        assert (tmp_path / run_live_session.TOOL_DIGEST_FIXTURE_REL).is_file()

    assert fixture.restoration_status() == {
        "restored": True,
        "cleanup_error_codes": (),
    }
    assert (tmp_path / "pico.toml").read_bytes() == original
    assert not (tmp_path / run_live_session.TOOL_DIGEST_FIXTURE_REL).exists()


def test_fixture_removes_dangling_digest_symlink_on_exit(tmp_path):
    original = b"ordinary = true\n"
    (tmp_path / "pico.toml").write_bytes(original)
    seed = tmp_path / "seed.md"
    seed.write_text("safe seed\n", encoding="utf-8")
    fixture = run_live_session.FixtureManager(tmp_path)
    fixture._seed_source = seed

    fixture.__enter__()
    digest = tmp_path / run_live_session.TOOL_DIGEST_FIXTURE_REL
    digest.unlink()
    digest.symlink_to(tmp_path / "missing-target")
    fixture.__exit__(None, None, None)

    assert not os.path.lexists(digest)
    assert fixture.restoration_status()["restored"] is True
