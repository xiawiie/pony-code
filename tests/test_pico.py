import json
import os
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import pytest

import pico as pico_pkg
import pico.cli.app as pico_cli
from pico.agent.loop import _commit_session, _plain_message
import pico.memory.service as memorylib
from pico.agent.messages import make_tool_pair, validate_messages
from pico.runtime.application import DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_MAX_STEPS
from pico.state.session_store import LEGACY_SESSION_FORMAT_VERSION, SessionStore
from pico import Pico
from pico.cli.app import build_welcome
from pico.workspace.context import WorkspaceContext
from benchmarks.support.fake_provider import FakeModelClient
from pico.providers.response import Response, StopReason
from pico.runtime.options import RuntimeOptions


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(approval_policy=approval_policy, **kwargs),
    )


def bound_fake_client(
    outputs,
    *,
    protocol_family="openai_responses",
    model="gpt-test",
    endpoint_hash_character="a",
):
    client = FakeModelClient(outputs)
    client.provider_binding = {
        "protocol_family": protocol_family,
        "model": model,
        "endpoint_hash": "sha256:" + endpoint_hash_character * 64,
    }
    return client


def set_raw_file_summary(agent, path, summary):
    memorylib.set_file_summary_dict(
        agent.session["memory"]["file_summaries"],
        path,
        summary,
        workspace_root=agent.root,
    )


# =============================================================================
# Agent integration smoke tests
# =============================================================================


def test_pico_constructor_uses_coding_agent_defaults(tmp_path):
    agent = build_agent(tmp_path, [])

    assert agent.max_steps == DEFAULT_MAX_STEPS == 12
    assert agent.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS == 16_384


def test_new_runtime_persists_current_messages_only(tmp_path):
    agent = build_agent(tmp_path, ["done"])

    assert agent.ask("q") == "done"

    rows = [
        json.loads(line)
        for line in Path(agent.session_path).read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["record_type"] == "session_header"
    assert rows[0]["format_version"] == 2
    assert all("history" not in row for row in rows)
    persisted = agent.session_store.load(agent.session["id"])
    validate_messages(persisted["messages"], require_meta=True)


def test_new_session_persists_provider_binding(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    client = bound_fake_client([])

    agent = Pico(
        model_client=client,
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(approval_policy="auto"),
    )

    assert agent.session["provider_binding"] == client.provider_binding
    assert (
        store.load(agent.session["id"])["provider_binding"] == client.provider_binding
    )


def test_resume_rejects_a_different_model_session_binding(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    original = Pico(
        model_client=bound_fake_client([]),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(approval_policy="auto"),
    )
    different = bound_fake_client(
        [],
        protocol_family="anthropic_messages",
        model="claude-test",
        endpoint_hash_character="b",
    )

    with pytest.raises(ValueError, match="model_session_mismatch"):
        Pico.from_session(
            model_client=different,
            workspace=workspace,
            session_store=store,
            session_id=original.session["id"],
            options=RuntimeOptions(approval_policy="auto"),
        )


def test_unbound_legacy_session_cannot_replay_provider_state(tmp_path):
    original = build_agent(tmp_path, [])
    original.session["messages"] = list(
        make_tool_pair(
            name="read_file",
            arguments={"path": "README.md"},
            tool_use_id="legacy-state-call",
            result_content="body",
            created_at="now",
            tool_status="ok",
            effect_class="read_only",
            provider_state=[
                {
                "type": "reasoning",
                "encrypted_content": "opaque-state",
                "summary": [],
                }
            ],
        )
    )
    original.session_store.save(original.session)

    with pytest.raises(ValueError, match="model_session_mismatch"):
        Pico.from_session(
            model_client=bound_fake_client(["done"]),
            workspace=original.workspace,
            session_store=original.session_store,
            session_id=original.session["id"],
            options=RuntimeOptions(approval_policy="auto"),
        )


def test_unbound_session_cannot_resume_with_a_bound_model(tmp_path):
    original = build_agent(tmp_path, [])
    client = bound_fake_client(["done"])
    with pytest.raises(ValueError, match="model_session_mismatch"):
        Pico.from_session(
            model_client=client,
            workspace=original.workspace,
            session_store=original.session_store,
            session_id=original.session["id"],
            options=RuntimeOptions(approval_policy="auto"),
        )


def test_commit_session_keeps_memory_and_disk_on_same_safe_payload(tmp_path):
    secret = "sk-session-secret-123456789"
    agent = build_agent(tmp_path, [])
    agent.memory.set_task_summary(secret)
    agent._sync_working_memory()

    _commit_session(agent, messages=(_plain_message("user", secret),))

    persisted = agent.session_store.load(agent.session["id"])
    assert secret not in json.dumps(agent.session)
    assert agent.session["messages"] == persisted["messages"]
    assert persisted["working_memory"] == {
        "task_summary": "",
        "recent_files": [],
    }
    assert secret not in json.dumps(agent.memory.to_dict())


def test_turn_start_sanitizes_before_memory_and_task_state(tmp_path):
    secret = "github_pat_A123456789012345678901234567890"
    agent = build_agent(tmp_path, ["safe"])

    agent.ask(secret)

    assert secret not in json.dumps(agent.memory.to_dict())
    assert secret not in json.dumps(agent.current_task_state.to_dict())


def test_programmatic_resume_sanitizes_process_secret_before_first_request(
    tmp_path,
    monkeypatch,
):
    secret = "opaque-process-value-123456789"
    monkeypatch.setenv("PICO_TEST_API_KEY", secret)
    original = build_agent(tmp_path, [])
    raw = dict(original.session)
    raw["messages"] = [
        {"role": "user", "content": secret, "_pico_meta": {"created_at": "test"}}
    ]
    raw["format_version"] = 1
    original.session_store.path(raw["id"]).unlink()
    legacy = original.session_store.legacy_path(raw["id"])
    legacy.write_text(json.dumps(raw), encoding="utf-8")
    legacy.chmod(0o600)
    client = FakeModelClient(["<final>safe</final>"])
    resume_store = SessionStore(original.session_store.root)

    resumed = Pico.from_session(
        model_client=client,
        workspace=original.workspace,
        session_store=resume_store,
        session_id=raw["id"],
        options=RuntimeOptions(approval_policy="auto"),
    )
    resumed.ask("continue")

    assert secret not in json.dumps(client.requests)
    assert secret not in json.dumps(resumed.session)
    assert isinstance(resumed.redaction_env, MappingProxyType)
    with pytest.raises(TypeError):
        resumed.redaction_env["MUTATE"] = "blocked"


def test_supplied_redaction_proxy_is_copied_before_backing_mutation(tmp_path):
    secret = "opaque-proxy-value-123456789"
    backing = {"PICO_TEST_API_KEY": secret}
    supplied = MappingProxyType(backing)
    agent = build_agent(
        tmp_path,
        [],
        redaction_env=supplied,
    )

    backing["PICO_TEST_API_KEY"] = "replacement-value-123456789"

    assert agent.redaction_env["PICO_TEST_API_KEY"] == secret
    assert agent.redaction_env is not supplied
    assert agent.redact_text(secret) == "<redacted>"


def test_delegate_reuses_snapshot_without_replacing_shared_store_redactors(
    tmp_path,
    monkeypatch,
):
    secret = "opaque-delegate-value-123456789"
    agent = build_agent(
        tmp_path,
        [],
        redaction_env=MappingProxyType({"PICO_TEST_API_KEY": secret}),
    )
    session_redactor = agent.session_store._redactor
    run_redactor = agent.run_store._redactor
    assert getattr(session_redactor, "__self__", None) is None
    assert getattr(run_redactor, "__self__", None) is None
    children = []

    def fake_ask(child, task):
        children.append(child)
        return "safe"

    monkeypatch.setattr(Pico, "ask", fake_ask)

    assert agent.spawn_delegate({"task": "inspect", "max_steps": 1}) == (
        "delegate_result:\nsafe"
    )

    assert children[0].redaction_env is agent.redaction_env
    assert agent.session_store._redactor is session_redactor
    assert agent.run_store._redactor is run_redactor
    safe = session_redactor({"payload": secret})
    assert secret not in json.dumps(safe)


def test_supplied_legacy_session_is_rejected_outside_store_migration(
    tmp_path,
):
    secret = "github_pat_A123456789012345678901234567890"
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    raw_session = {
        "record_type": "session",
        "format_version": LEGACY_SESSION_FORMAT_VERSION,
        "id": "direct-raw",
        "created_at": "2026-01-01T00:00:00+00:00",
        "workspace_root": str(tmp_path),
        "messages": [{"role": "user", "content": secret, "_pico_meta": {}}],
        "working_memory": {"task_summary": secret, "recent_files": []},
        "memory": {},
        "recently_recalled": [],
        "checkpoints": {},
        "resume_state": {},
        "recovery": {},
        "runtime_identity": {},
    }

    with pytest.raises(ValueError, match="current session"):
        Pico(
            model_client=FakeModelClient([]),
            workspace=workspace,
            session_store=store,
            session=raw_session,
            options=RuntimeOptions(approval_policy="auto"),
        )


def test_runtime_rejects_dead_prompt_cache_feature_flag(tmp_path):
    with pytest.raises(ValueError, match="unsupported feature flag"):
        build_agent(tmp_path, [], feature_flags={"prompt_cache": True})


def test_repeated_tool_detection_reads_canonical_tool_use_blocks(tmp_path):
    agent = build_agent(tmp_path, [])
    pairs = []
    for index, path in enumerate(("a.py", "b.py", "a.py", "b.py")):
        pairs.extend(
            make_tool_pair(
            name="read_file",
            arguments={"path": path},
            tool_use_id=f"tu_{index}",
            result_content="body",
            created_at="t",
            tool_status="ok",
            effect_class="read_only",
            )
        )
    agent.session["messages"].extend(pairs)

    assert agent.repeated_tool_call("read_file", {"path": "a.py"}) is True
    assert agent.repeated_tool_call("read_file", {"path": "c.py"}) is False


def test_reset_clears_transient_v3_state_and_preserves_audit_items(tmp_path):
    agent = build_agent(tmp_path, ["done"])
    agent.ask("q")
    session_id = agent.session["id"]
    agent.session["recently_recalled"] = ["note"]
    agent.session["_recall_errors"] = {"count": 2, "last": "x"}
    agent.session["working_memory"] = {
        "task_summary": "goal",
        "recent_files": ["a.py"],
    }
    agent.session["memory"] = {"file_summaries": {"a.py": {"summary": "fact"}}}
    agent.session["checkpoints"] = {
        "current_id": "c1",
        "items": {"c1": {"checkpoint_id": "c1"}},
    }
    agent.session["resume_state"] = {"status": "full-valid"}
    agent.session["recovery"] = {"current_checkpoint_id": "r1"}

    agent.reset()

    assert agent.session["id"] == session_id
    assert agent.session["messages"] == []
    assert agent.session["recently_recalled"] == []
    assert "_recall_errors" not in agent.session
    assert agent.session["working_memory"] == {"task_summary": "", "recent_files": []}
    assert agent.session["memory"] == {"file_summaries": {}}
    assert agent.session["checkpoints"]["current_id"] == ""
    assert agent.session["checkpoints"]["items"] == {"c1": {"checkpoint_id": "c1"}}
    assert agent.session["resume_state"] == {}
    assert agent.session["recovery"]["current_checkpoint_id"] == ""


def test_agent_runs_tool_then_final(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {"name": "read_file", "args": {"path":"hello.txt","start":1,"end":2}},
            "Read the file successfully.",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Read the file successfully."
    assert any(
        message["role"] == "assistant"
        and isinstance(message["content"], list)
        and message["content"][0].get("type") == "tool_use"
        and message["content"][0].get("name") == "read_file"
        for message in agent.session["messages"]
    )
    assert "hello.txt" in agent.session["working_memory"]["recent_files"]
    assert "hello.txt" in agent.session["memory"]["file_summaries"]


def test_agent_updates_task_summary_on_each_request(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "First pass.",
            "Second pass.",
        ],
    )

    assert agent.ask("First request") == "First pass."
    assert agent.session["working_memory"]["task_summary"] == "First request"

    assert agent.ask("Second request") == "Second pass."
    assert agent.session["working_memory"]["task_summary"] == "Second request"


def test_agent_stores_file_summaries_without_episodic_notes(tmp_path):
    (tmp_path / "facts.txt").write_text("deploy key is red\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {"name": "read_file", "args": {"path":"facts.txt","start":1,"end":1}},
            "Done.",
            "It is red.",
        ],
    )

    assert agent.ask("Read the file and remember the fact") == "Done."
    assert "facts.txt" in agent.session["working_memory"]["recent_files"]
    assert "deploy key is red" in agent.session["memory"]["file_summaries"]["facts.txt"]
    checkpoint = agent.current_checkpoint()
    assert any(
        item.get("path") == "facts.txt"
        and "deploy key is red" in item.get("summary", "")
        for item in checkpoint["key_files"]
    )
    assert "episodic_notes" not in agent.session["memory"]
    assert "notes" not in agent.session["memory"]

    resumed = Pico.from_session(
        model_client=FakeModelClient(["It is red."]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        options=RuntimeOptions(approval_policy="auto"),
    )

    assert resumed.ask("What color is the deploy key?") == "It is red."
    assert "episodic_notes" not in resumed.session["memory"]
    assert "notes" not in resumed.session["memory"]


def test_file_summary_cache_is_invalidated_on_out_of_band_edit_and_path_spelling(
    tmp_path,
):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    set_raw_file_summary(agent, "./sample.txt", "sample.txt: alpha")
    agent.memory.remember_file("./sample.txt")
    agent._sync_working_memory()
    agent.session_store.save(agent.session)
    assert agent.session["memory"]["file_summaries"]["sample.txt"]["freshness"]

    file_path.write_text("beta\n", encoding="utf-8")

    resumed = Pico.from_session(
        model_client=FakeModelClient([]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        options=RuntimeOptions(approval_policy="auto"),
    )

    assert "sample.txt" not in resumed.session["memory"]["file_summaries"]


def test_agent_retries_after_empty_model_output(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "Recovered after retry.",
        ],
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after retry."
    notice = "model returned no actionable content"
    assert not any(notice in str(item["content"]) for item in agent.session["messages"])
    feedback_requests = [
        index
        for index, request in enumerate(agent.model_client.requests)
        if "<pico:runtime_feedback>" in json.dumps(request)
    ]
    assert feedback_requests == [1]
    assert notice in json.dumps(agent.model_client.requests[1])


def test_agent_retries_after_malformed_tool_payload(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                        "type": "tool_use",
                        "id": "bad_call",
                        "name": "read_file",
                        "input": "bad",
                    }
                ],
            ),
            {"name": "read_file", "args": {"path":"hello.txt","start":1,"end":1}},
            "Recovered after malformed tool output.",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Recovered after malformed tool output."
    assert any(
        message["role"] == "assistant"
        and isinstance(message["content"], list)
        and message["content"][0].get("type") == "tool_use"
        and message["content"][0].get("name") == "read_file"
        for message in agent.session["messages"]
    )
    notice = "native tool call had an invalid name or arguments object"
    assert not any(notice in str(item["content"]) for item in agent.session["messages"])
    feedback_requests = [
        index
        for index, request in enumerate(agent.model_client.requests)
        if "<pico:runtime_feedback>" in json.dumps(request)
    ]
    assert feedback_requests == [1]
    assert notice in json.dumps(agent.model_client.requests[1])


def test_agent_never_executes_text_tool_markup(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.py"><content>print("hi")\n</content></tool>',
            "Done.",
        ],
    )

    answer = agent.ask("Create hello.py")

    assert answer.startswith('<tool name="write_file"')
    assert not (tmp_path / "hello.py").exists()


def test_one_protocol_correction_can_recover(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "Recovered after one correction.",
        ],
        max_steps=1,
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after one correction."


def test_agent_saves_and_resumes_session(tmp_path):
    agent = build_agent(tmp_path, ["First pass."])
    assert agent.ask("Start a session") == "First pass."

    resumed = Pico.from_session(
        model_client=FakeModelClient(["Resumed."]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        options=RuntimeOptions(approval_policy="auto"),
    )

    assert resumed.session["messages"][0]["content"] == "Start a session"
    assert resumed.ask("Continue") == "Resumed."


def test_delegate_uses_child_agent(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            {"name": "delegate", "args": {"task":"inspect README","max_steps":2}},
            "Child result.",
            "Parent incorporated the child result.",
        ],
    )

    answer = agent.ask("Use delegation")

    assert answer == "Parent incorporated the child result."
    tool_results = [
        message["content"][0]
        for message in agent.session["messages"]
        if message["role"] == "user"
        and isinstance(message["content"], list)
        and message["content"][0].get("type") == "tool_result"
    ]
    assert "delegate_result" in tool_results[0]["content"]


def test_patch_file_replaces_exact_match(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello world\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "patch_file",
        {
            "path": "sample.txt",
            "old_text": "world",
            "new_text": "agent",
        },
    )

    assert result == "patched sample.txt"
    assert file_path.read_text(encoding="utf-8") == "hello agent\n"


def test_invalid_risky_tool_does_not_prompt_for_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")

    with patch("builtins.input") as mock_input:
        result = agent.run_tool("write_file", {})

    assert result.startswith("error: invalid arguments for write_file: 'path'")
    assert 'example: {"name":"write_file","arguments":' in result
    mock_input.assert_not_called()


def test_list_files_hides_internal_agent_state(tmp_path):
    agent = build_agent(tmp_path, [])
    (tmp_path / ".pico").mkdir(exist_ok=True)
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")

    result = agent.run_tool("list_files", {})

    assert ".pico" not in result
    assert ".git" not in result
    assert "[F] hello.txt" in result


def test_repeated_identical_tool_call_is_rejected(tmp_path):
    agent = build_agent(tmp_path, [])
    for index in range(2):
        agent.session["messages"].extend(
            make_tool_pair(
            name="list_files",
            arguments={},
            tool_use_id=f"tu_{index}",
            result_content="(empty)",
            created_at=str(index),
            tool_status="ok",
            effect_class="read_only",
            )
        )

    result = agent.run_tool("list_files", {})

    assert (
        result
        == "error: repeated identical tool call for list_files; choose a different tool or return a final answer"
    )


def test_repeated_tool_call_rejects_short_alternating_loops(tmp_path):
    agent = build_agent(tmp_path, [])
    calls = [
        ("list_files", {}, "(empty)"),
        ("read_file", {"path": "README.md", "start": 1, "end": 1}, "demo"),
        ("list_files", {}, "(empty)"),
        ("read_file", {"path": "README.md", "start": 1, "end": 1}, "demo"),
    ]
    for index, (name, arguments, content) in enumerate(calls):
        agent.session["messages"].extend(
            make_tool_pair(
            name=name,
            arguments=arguments,
            tool_use_id=f"tu_{index}",
            result_content=content,
            created_at=str(index),
            tool_status="ok",
            effect_class="read_only",
            )
        )

    result = agent.run_tool("list_files", {})

    assert (
        result
        == "error: repeated identical tool call for list_files; choose a different tool or return a final answer"
    )


def test_welcome_screen_keeps_box_shape_for_long_paths(tmp_path):
    deep = (
        tmp_path
        / "very"
        / "long"
        / "path"
        / "for"
        / "the"
        / "pico"
        / "agent"
        / "welcome"
        / "screen"
    )
    deep.mkdir(parents=True)
    agent = build_agent(deep, [])

    welcome = build_welcome(agent, model="qwen3.5:4b", host="http://127.0.0.1:11434")
    lines = welcome.splitlines()

    assert len(lines) >= 5
    assert len({len(line) for line in lines}) == 1
    assert "..." in welcome
    assert "(  o o  )" in welcome
    assert "MINI-CODING-AGENT" not in welcome
    assert "MINI CODING AGENT" not in welcome
    assert "pico" in welcome
    assert "local coding agent" in welcome
    assert "// READY" not in welcome
    assert "SLASH" not in welcome
    assert "READY      " not in welcome
    assert "commands: Commands:" not in welcome


# =============================================================================
# Build agent / fixed model configuration tests
# =============================================================================


def test_build_arg_parser_has_no_model_backend_selection_flags(tmp_path):
    parser = pico_cli.build_arg_parser()
    destinations = {action.dest for action in parser._actions}

    assert {
        "provider",
        "profile",
        "auth_mode",
        "model",
        "base_url",
        "host",
        "connection",
        "api",
        "api_key_env",
    }.isdisjoint(destinations)


def test_build_agent_uses_resolved_anthropic_client_and_project_env(tmp_path):
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=anthropic\n"
        "PICO_MODEL=claude-sonnet-4-6\n"
        "PICO_API_URL=https://gateway.example/v1\n"
        "PICO_API_KEY=sk-project\n",
        encoding="utf-8",
    )
    args = pico_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True):
        with patch("pico.cli.assembly.build_transport_client") as model_client:
            fake_client = model_client.return_value
            agent = pico_cli.build_agent(args)

    model_client.assert_called_once()
    assert model_client.call_args.args == ("anthropic_messages",)
    assert model_client.call_args.kwargs == {
        "model": "claude-sonnet-4-6",
        "base_url": "https://gateway.example/v1",
        "api_key": "sk-project",
        "timeout": 300,
        "auth_mode": "x-api-key",
        "capabilities": {
            "prompt_cache": True,
            "strict_tools": True,
            "parallel_tool_control": True,
        },
    }
    assert agent.model_client is fake_client


def test_build_agent_uses_process_env_when_project_env_is_missing(tmp_path):
    args = pico_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(
        os.environ,
        {
            "HOME": str(tmp_path),
            "PICO_PROVIDER": "anthropic",
            "PICO_MODEL": "claude-sonnet-4-6",
            "PICO_API_URL": "https://process.example/v1",
            "PICO_API_KEY": "sk-process",
        },
        clear=True,
    ):
        with patch("pico.cli.assembly.build_transport_client") as model_client:
            pico_cli.build_agent(args)

    assert model_client.call_args.kwargs["base_url"] == "https://process.example/v1"
    assert model_client.call_args.kwargs["api_key"] == "sk-process"


def test_build_agent_switches_provider_from_generic_environment(tmp_path):
    args = pico_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(
        os.environ,
        {
            "HOME": str(tmp_path),
            "PICO_PROVIDER": "openai",
            "PICO_MODEL": "gpt-test",
            "PICO_API_URL": "https://api.openai.com/v1",
            "PICO_API_KEY": "sk-openai",
        },
        clear=True,
    ):
        with patch("pico.cli.assembly.build_transport_client") as model_client:
            pico_cli.build_agent(args)

    assert model_client.call_args.args == ("openai_responses",)
    assert model_client.call_args.kwargs["model"] == "gpt-test"
    assert model_client.call_args.kwargs["api_key"] == "sk-openai"
    assert model_client.call_args.kwargs["auth_mode"] == "bearer"


# =============================================================================
# Runtime/report/resume tests
# =============================================================================
# Runtime/report/resume tests moved to tests/test_runtime_report.py.


# =============================================================================
# Build agent / arg parser / packaging tests
# =============================================================================


def test_public_api_exports_resolve_through_package_path():
    assert callable(build_welcome)
    assert Pico is not None
    assert SessionStore is not None
    assert WorkspaceContext is not None
    assert Path(pico_pkg.__file__).as_posix().endswith("/pico/__init__.py")


def test_package_import_surface_excludes_cli_entrypoints():
    assert not hasattr(pico_pkg, "main")
    assert not hasattr(pico_pkg, "build_agent")
    assert not hasattr(pico_pkg, "build_arg_parser")


def test_pico_initializes_recovery_components(tmp_path):
    agent = build_agent(tmp_path, outputs=["ok"])

    assert agent.checkpoint_store.root == tmp_path / ".pico" / "checkpoints"
    assert agent.tool_change_recorder.store is agent.checkpoint_store
    assert agent.recovery_checkpoint_writer.store is agent.checkpoint_store
    assert agent.recovery_manager.store is agent.checkpoint_store


def test_module_execution_help_works():
    result = subprocess.run(
        [sys.executable, "-m", "pico", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
