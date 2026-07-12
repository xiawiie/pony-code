import copy
import json
import logging
from unittest.mock import Mock

import pytest

import pico.agent_loop as agent_loop_module
from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient
from pico.agent_loop import AgentLoop
from pico.providers._shared import _ProviderFailure
from pico.providers.response import Response, StopReason


class NativeScriptProvider:
    supports_prompt_cache = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.last_completion_metadata = {"input_tokens": 999999}

    def complete(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.calls.append(
            {
                "system": system,
                "tools": tools,
                "messages": messages,
                "max_tokens": max_tokens,
                "cache_breakpoints": cache_breakpoints,
            }
        )
        return self.responses.pop(0)


class RaisingProvider:
    supports_prompt_cache = False

    def __init__(self, error):
        self.error = error
        self.calls = []

    def complete(
        self,
        *,
        system,
        tools,
        messages,
        max_tokens,
        cache_breakpoints=None,
    ):
        self.calls.append(
            {
                "system": system,
                "tools": tools,
                "messages": messages,
                "max_tokens": max_tokens,
                "cache_breakpoints": cache_breakpoints,
            }
        )
        raise self.error


class EvidenceScriptProvider:
    supports_prompt_cache = False

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.last_transport_attempts = 0

    def complete(
        self,
        *,
        system,
        tools,
        messages,
        max_tokens,
        cache_breakpoints=None,
    ):
        self.last_transport_attempts = 1
        self.calls.append(copy.deepcopy(messages))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def build_native_agent(tmp_path, provider, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=provider,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


def build_agent(tmp_path, outputs, max_steps=6):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=max_steps,
    )


def read_trace(agent):
    return [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]


def test_agent_loop_runs_same_control_flow_as_pico_ask(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Done.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "Done."
    assert agent.current_task_state.status == "completed"
    assert agent.run_store.report_path(agent.current_task_state.run_id).exists()


def test_transient_provider_failures_retry_in_agent_loop_with_explicit_origins(
    tmp_path,
    monkeypatch,
):
    provider = EvidenceScriptProvider([
        _ProviderFailure(
            "OpenAI-compatible request failed: timeout",
            code="timeout",
            retryable=True,
        ),
        _ProviderFailure(
            "OpenAI-compatible request failed with HTTP 503",
            code="http_5xx",
            http_status=503,
            retryable=True,
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
        ),
    ])
    delays = []
    monkeypatch.setattr(agent_loop_module.time, "sleep", delays.append)
    agent = build_native_agent(tmp_path, provider)

    assert agent.ask("recover") == "done"
    assert delays == [0.5, 1.0]
    assert len(provider.calls) == 3
    events = read_trace(agent)
    requested = [event for event in events if event["event"] == "model_requested"]
    failed = [event for event in events if event["event"] == "model_failed"]
    turns = [event for event in events if event["event"] == "model_turn"]
    assert [event["attempt_origin"] for event in requested] == [
        "initial",
        "model_retry",
        "model_retry",
    ]
    assert [event["reason_code"] for event in failed] == ["timeout", "http_5xx"]
    assert turns[0]["attempt_origin"] == "model_retry"
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["model_execution"] == {
        "model_attempts": 3,
        "model_turns": 1,
        "model_failures": 2,
        "model_retries": 2,
        "attempt_origin_counts": {
            "initial": 1,
            "tool_followup": 0,
            "retry_action": 0,
            "model_retry": 2,
        },
        "transport_attempts": 3,
        "transport_retries": 0,
        "transport_evidence_complete": True,
        "failure_reason_counts": {"timeout": 1, "http_5xx": 1},
    }


def test_nonretryable_provider_failure_is_not_replayed(tmp_path, monkeypatch):
    failure = _ProviderFailure(
        "OpenAI-compatible request failed with HTTP 429",
        code="rate_limited",
        http_status=429,
    )
    provider = EvidenceScriptProvider([failure])
    sleep = Mock()
    monkeypatch.setattr(agent_loop_module.time, "sleep", sleep)
    agent = build_native_agent(tmp_path, provider)

    with pytest.raises(RuntimeError) as caught:
        agent.ask("do not replay")

    assert caught.value is failure
    assert len(provider.calls) == 1
    sleep.assert_not_called()


def test_model_retry_preserves_retry_action_feedback(tmp_path, monkeypatch):
    provider = EvidenceScriptProvider([
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "<tool>{bad}</tool>"}],
            usage={},
        ),
        _ProviderFailure(
            "Anthropic-compatible request failed: network_error",
            code="network_error",
            retryable=True,
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={},
        ),
    ])
    monkeypatch.setattr(agent_loop_module.time, "sleep", lambda _delay: None)
    agent = build_native_agent(tmp_path, provider)

    assert agent.ask("recover protocol") == "done"
    assert "pico:runtime_feedback" not in json.dumps(provider.calls[0])
    assert "pico:runtime_feedback" in json.dumps(provider.calls[1])
    assert "pico:runtime_feedback" in json.dumps(provider.calls[2])
    requested = [
        event for event in read_trace(agent) if event["event"] == "model_requested"
    ]
    assert [event["attempt_origin"] for event in requested] == [
        "initial",
        "retry_action",
        "model_retry",
    ]


def test_retry_action_allows_only_one_protocol_correction_per_run(tmp_path):
    provider = EvidenceScriptProvider([
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "<tool>{bad}</tool>"}],
            usage={},
        ),
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "tu_read",
                "name": "read_file",
                "input": {"path": "README.md"},
            }],
            usage={},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "<tool>{bad-again}</tool>"}],
            usage={},
        ),
    ])
    agent = build_native_agent(tmp_path, provider, max_steps=2)

    answer = agent.ask("one correction only")

    assert answer.startswith("Stopped after repeated malformed model responses")
    assert len(provider.calls) == 3
    assert agent.current_task_state.attempts == 3


def test_missing_custom_transport_evidence_is_null_in_report(tmp_path):
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={},
        )
    ])
    agent = build_native_agent(tmp_path, provider)

    assert agent.ask("custom") == "done"
    execution = agent.run_store.load_report(agent.current_task_state.run_id)[
        "model_execution"
    ]
    assert execution["transport_evidence_complete"] is False
    assert execution["transport_attempts"] is None
    assert execution["transport_retries"] is None


def test_pico_ask_delegates_to_agent_loop(tmp_path):
    agent = build_agent(tmp_path, ["<final>Facade works.</final>"])

    assert agent.ask("Use facade") == "Facade works."


def test_rejected_tool_action_never_creates_verification_evidence():
    assert agent_loop_module._verification_evidence_for_tool(
        "run_shell",
        {"tool_status": "rejected"},
    ) is None


def test_agent_loop_decodes_native_action_and_aggregates_response_usage_only(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    provider = NativeScriptProvider(
        [
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_native",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    }
                ],
                usage={
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                    "cached_tokens": 3,
                    "cache_creation_input_tokens": 4,
                    "cache_read_input_tokens": 3,
                    "cache_hit": True,
                },
            ),
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "done"}],
                usage={"input_tokens": 20, "output_tokens": 5, "total_tokens": None},
            ),
        ]
    )
    agent = build_native_agent(tmp_path, provider)

    assert agent.ask("read and finish") == "done"

    events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    decoded = [event for event in events if event["event"] == "action_decoded"]
    turns = [event for event in events if event["event"] == "model_turn"]
    report = agent.run_store.load_report(agent.current_task_state.run_id)

    assert decoded[0]["action_type"] == "tool"
    assert decoded[0]["origin"] == "native_tool_use"
    assert decoded[1]["action_type"] == "final"
    assert [turn["completion_usage"]["input_tokens"] for turn in turns] == [10, 20]
    assert report["completion_usage_totals"]["input_tokens"] == 30
    assert report["completion_usage_totals"]["output_tokens"] == 7
    assert report["completion_usage_totals"]["total_tokens"] == 37
    assert report["completion_usage_totals"]["cache_hit"] is True
    assert report["completion_usage_totals"]["input_tokens"] != 999999


def test_native_multiple_tool_response_executes_only_first_and_traces_ignored_count(
    tmp_path,
):
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[
                {
                    "type": "tool_use",
                    "id": "tu_first",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                },
                {
                    "type": "tool_use",
                    "id": "tu_ignored",
                    "name": "write_file",
                    "input": {"path": "ignored.txt", "content": "no\n"},
                },
            ],
            usage={},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    first_runner = Mock(return_value="# README.md\n1: demo")
    ignored_runner = Mock(return_value="must not run")
    agent.tools["read_file"]["run"] = first_runner
    agent.tools["write_file"]["run"] = ignored_runner

    assert agent.ask("use one tool") == "done"

    first_runner.assert_called_once()
    ignored_runner.assert_not_called()
    assert not (tmp_path / "ignored.txt").exists()
    assert agent.checkpoint_store.list_tool_change_records() == []
    events = read_trace(agent)
    decoded = [event for event in events if event["event"] == "action_decoded"]
    assert decoded[0]["action_type"] == "tool"
    assert decoded[0]["ignored_tool_count"] == 1
    model_turns = [event for event in events if event["event"] == "model_turn"]
    assert model_turns[0]["ignored_tool_count"] == 1
    tool_uses = []
    for message in agent.session["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            tool_uses.extend(
                block for block in content if block.get("type") == "tool_use"
            )
    assert [block["id"] for block in tool_uses] == ["tu_first"]


def test_ordinary_workspace_tool_error_commits_pair_consumes_step_and_finishes(
    tmp_path,
    monkeypatch,
):
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "tu_error",
                "name": "write_file",
                "input": {"path": "failed.txt", "content": "no\n"},
            }],
            usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "recovered"}],
            usage={"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    runner = Mock(side_effect=RuntimeError("ordinary tool failure"))
    agent.tools["write_file"]["run"] = runner
    saved_transcripts = []
    original_save = agent.session_store.save

    def capture_save(session):
        saved_transcripts.append(copy.deepcopy(session.get("messages") or []))
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", capture_save)

    assert agent.ask("attempt write then finish") == "recovered"

    runner.assert_called_once()
    assert len(provider.calls) == 2
    assert agent.current_task_state.tool_steps == 1
    assert agent.current_task_state.stop_reason == "final_answer_returned"
    tool_result = next(
        message
        for message in agent.session["messages"]
        if isinstance(message.get("content"), list)
        and message["content"]
        and message["content"][0].get("type") == "tool_result"
    )
    assert tool_result["content"][0]["tool_use_id"] == "tu_error"
    assert tool_result["content"][0]["is_error"] is True
    assert tool_result["_pico_meta"]["tool_status"] == "error"
    tool_change_id = tool_result["_pico_meta"]["tool_change_id"]
    tool_change = agent.checkpoint_store.load_tool_change_record(tool_change_id)
    assert tool_change["status"] == "error"
    assert tool_change["error"]["code"] == "tool_failed"
    checkpoint_id = agent.current_task_state.recovery_checkpoint_id
    checkpoint = agent.checkpoint_store.load_checkpoint_record(checkpoint_id)
    assert checkpoint["tool_change_ids"] == [tool_change_id]
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["status"] == "completed"
    assert report["tool_steps"] == 1
    assert report["completion_usage_totals"]["total_tokens"] == 7
    executed = [event for event in read_trace(agent) if event["event"] == "tool_executed"]
    assert executed[0]["tool_status"] == "error"
    snapshots_with_error_pair = []
    for messages in saved_transcripts:
        use_ids = []
        result_ids = []
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            use_ids.extend(
                block.get("id")
                for block in content
                if block.get("type") == "tool_use"
            )
            result_ids.extend(
                block.get("tool_use_id")
                for block in content
                if block.get("type") == "tool_result"
            )
        if "tu_error" in use_ids or "tu_error" in result_ids:
            snapshots_with_error_pair.append((use_ids, result_ids))

    assert snapshots_with_error_pair
    assert snapshots_with_error_pair[0][0].count("tu_error") == 1
    assert snapshots_with_error_pair[0][1].count("tu_error") == 1
    assert all(
        use_ids.count("tu_error") == 1 and result_ids.count("tu_error") == 1
        for use_ids, result_ids in snapshots_with_error_pair
    )


def test_tool_pair_is_written_by_one_session_save_without_orphan(tmp_path, monkeypatch):
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "tu_pair",
                "name": "read_file",
                "input": {"path": "README.md"},
            }],
            usage={},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    saved_transcripts = []
    original_save = agent.session_store.save

    def spy_save(session):
        saved_transcripts.append(copy.deepcopy(session["messages"]))
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", spy_save)

    assert agent.ask("read") == "done"

    writes_with_pair = [
        messages
        for messages in saved_transcripts
        if any(
            message.get("role") == "assistant"
            and isinstance(message.get("content"), list)
            and message["content"][0].get("type") == "tool_use"
            for message in messages
        )
    ]
    assert writes_with_pair
    first = writes_with_pair[0]
    tool_index = next(
        index
        for index, message in enumerate(first)
        if isinstance(message.get("content"), list)
        and message["content"][0].get("type") == "tool_use"
    )
    assert first[tool_index]["content"][0]["id"] == "tu_pair"
    assert first[tool_index + 1]["content"][0]["tool_use_id"] == "tu_pair"
    assert not any(
        messages[-1].get("role") == "assistant"
        and isinstance(messages[-1].get("content"), list)
        and messages[-1]["content"][0].get("type") == "tool_use"
        for messages in saved_transcripts
    )


def test_side_effect_then_pair_save_failure_stops_before_another_provider_call(
    tmp_path,
    monkeypatch,
):
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "tu_write",
                "name": "write_file",
                "input": {"path": "created.txt", "content": "created\n"},
            }],
            usage={},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "must not be requested"}],
            usage={},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    original_save = agent.session_store.save

    def fail_pair(session):
        if any(
            isinstance(message.get("content"), list)
            and message["content"]
            and message["content"][0].get("type") == "tool_use"
            for message in session.get("messages", [])
        ):
            raise OSError("pair save failed")
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", fail_pair)

    with pytest.raises(OSError, match="pair save failed"):
        agent.ask("write file")

    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created\n"
    assert len(provider.calls) == 1
    assert agent.current_task_state.stop_reason == "persistence_error"
    assert agent.current_task_state.status == "failed"
    messages = agent.session["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "write file"
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["_pico_meta"]["origin"] == "runtime_terminal"
    assert messages[-1]["content"] == (
        "This turn stopped because session state could not be saved."
    )
    assert all(
        not (
            isinstance(message.get("content"), list)
            and message["content"]
            and message["content"][0].get("type") == "tool_use"
        )
        for message in messages
    )
    assert agent.current_task_state.recovery_checkpoint_id


def test_pair_save_failure_restores_pre_tool_memory(tmp_path, monkeypatch):
    (tmp_path / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    (tmp_path / "target.txt").write_text("target\n", encoding="utf-8")
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "tu_read",
                "name": "read_file",
                "input": {"path": "target.txt"},
            }],
            usage={},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    agent.update_memory_after_tool(
        "read_file",
        {"path": "baseline.txt"},
        "# baseline.txt\n   1: baseline",
    )
    baseline_summaries = copy.deepcopy(agent.session["memory"]["file_summaries"])
    original_save = agent.session_store.save

    def fail_pair(session):
        if any(
            isinstance(message.get("content"), list)
            and message["content"]
            and message["content"][0].get("type") == "tool_use"
            for message in session.get("messages", [])
        ):
            raise OSError("pair save failed")
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", fail_pair)

    with pytest.raises(OSError, match="pair save failed"):
        agent.ask("read target")

    persisted = agent.session_store.load(agent.session["id"])
    assert persisted["messages"][-1]["_pico_meta"]["origin"] == "runtime_terminal"
    assert persisted["working_memory"]["recent_files"] == ["baseline.txt"]
    assert persisted["memory"]["file_summaries"] == baseline_summaries
    assert agent.session["working_memory"] == persisted["working_memory"]
    assert agent.session["memory"] == persisted["memory"]
    assert agent.current_task_state.stop_reason == "persistence_error"
    assert len(provider.calls) == 1


def test_pair_save_primary_error_survives_terminal_persistence_failure(
    tmp_path,
    monkeypatch,
):
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "tu_write",
                "name": "write_file",
                "input": {"path": "created.txt", "content": "created\n"},
            }],
            usage={},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    original_save = agent.session_store.save
    user_turn_saved = False

    def fail_pair_then_terminal(session):
        nonlocal user_turn_saved
        has_tool_use = any(
            isinstance(message.get("content"), list)
            and message["content"]
            and message["content"][0].get("type") == "tool_use"
            for message in session.get("messages", [])
        )
        if has_tool_use:
            raise OSError("pair save failed")
        if user_turn_saved:
            raise RuntimeError("terminal persistence failed")
        user_turn_saved = True
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", fail_pair_then_terminal)

    with pytest.raises(OSError, match="pair save failed"):
        agent.ask("write file")

    assert len(provider.calls) == 1
    assert all(
        not (
            isinstance(message.get("content"), list)
            and message["content"]
            and message["content"][0].get("type") == "tool_use"
        )
        for message in agent.session["messages"]
    )


def test_agent_loop_emits_focused_recovery_trace_events(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])

    agent.ask("say done")

    trace_text = agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8")
    assert '"event": "run_started"' in trace_text
    assert '"event": "model_turn"' in trace_text
    assert '"event": "checkpoint_created"' in trace_text


def test_recovery_checkpoint_uses_distinct_trace_event(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"note.txt","content":"after\\n"}}</tool>',
            "<final>done</final>",
        ],
    )

    agent.ask("write note")

    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    recovery_events = [event for event in trace_events if event["event"] == "recovery_checkpoint_created"]

    assert recovery_events
    assert recovery_events[0]["checkpoint_id"] == agent.current_task_state.recovery_checkpoint_id
    assert not any(
        event["event"] == "checkpoint_created" and event.get("checkpoint_kind") == "recovery"
        for event in trace_events
    )


def test_model_error_marks_run_failed_and_writes_report(tmp_path):
    agent = build_agent(tmp_path, [])

    with pytest.raises(RuntimeError, match="fake model ran out of outputs"):
        agent.ask("trigger backend failure")

    task_state = agent.current_task_state
    assert task_state.status == "failed"
    assert task_state.stop_reason == "model_error"
    assert agent.run_store.report_path(task_state).exists()

    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(task_state).read_text(encoding="utf-8").splitlines()
    ]
    assert trace_events[-1]["event"] == "run_finished"
    assert trace_events[-1]["status"] == "failed"


@pytest.mark.parametrize(
    (
        "case",
        "expected_status",
        "expected_reason",
        "expected_attempts",
        "expected_tool_steps",
        "expected_calls",
        "expected_total_tokens",
        "runtime_terminal_count",
    ),
    [
        ("final", "completed", "final_answer_returned", 1, 0, 1, 2, 0),
        ("step_limit", "stopped", "step_limit_reached", 1, 1, 1, 2, 0),
        ("retry_limit", "stopped", "retry_limit_reached", 2, 0, 2, 4, 0),
        ("model_error", "failed", "model_error", 1, 0, 1, 0, 1),
        ("preflight_error", "failed", "runtime_error", 0, 0, 0, 0, 1),
        ("persistence_error", "failed", "persistence_error", 1, 0, 1, 2, 1),
        ("interrupt", "stopped", "interrupted", 1, 0, 1, 0, 1),
    ],
)
def test_terminal_path_matrix_persists_exactly_one_finalization(
    tmp_path,
    monkeypatch,
    case,
    expected_status,
    expected_reason,
    expected_attempts,
    expected_tool_steps,
    expected_calls,
    expected_total_tokens,
    runtime_terminal_count,
):
    usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    primary = None
    max_steps = 1
    if case == "final":
        provider = NativeScriptProvider([
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "done"}],
                usage=usage,
            )
        ])
    elif case == "step_limit":
        provider = NativeScriptProvider([
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[{
                    "type": "tool_use",
                    "id": "tu_limit",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                }],
                usage=usage,
            )
        ])
    elif case == "retry_limit":
        provider = NativeScriptProvider([
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": f"<tool>{{bad-{index}}}</tool>"}],
                usage=usage,
            )
            for index in range(5)
        ])
    elif case == "model_error":
        primary = ValueError("provider failed")
        provider = RaisingProvider(primary)
    elif case == "preflight_error":
        primary = ValueError("preflight failed")
        provider = NativeScriptProvider([])
    elif case == "persistence_error":
        primary = OSError("assistant save failed")
        provider = NativeScriptProvider([
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "done"}],
                usage=usage,
            )
        ])
    else:
        primary = KeyboardInterrupt("interrupted")
        provider = RaisingProvider(primary)

    agent = build_native_agent(tmp_path / case, provider, max_steps=max_steps)
    if case == "preflight_error":
        monkeypatch.setattr(agent, "refresh_prefix", Mock(side_effect=primary))
    elif case == "persistence_error":
        original_save = agent.session_store.save

        def fail_final_answer(session):
            messages = session.get("messages") or []
            last = messages[-1] if messages else {}
            if (
                last.get("role") == "assistant"
                and (last.get("_pico_meta") or {}).get("origin")
                != "runtime_terminal"
            ):
                raise primary
            return original_save(session)

        monkeypatch.setattr(agent.session_store, "save", fail_final_answer)

    if primary is None:
        answer = agent.ask("exercise terminal path")
        if case == "final":
            assert answer == "done"
        else:
            assert "Stopped after" in answer
    else:
        with pytest.raises(BaseException) as caught:
            agent.ask("exercise terminal path")
        assert caught.value is primary

    state = agent.current_task_state
    assert state.status == expected_status
    assert state.stop_reason == expected_reason
    assert state.attempts == expected_attempts
    assert state.tool_steps == expected_tool_steps
    assert len(provider.calls) == expected_calls
    events = read_trace(agent)
    assert len([event for event in events if event["event"] == "run_finished"]) == 1
    report = agent.run_store.load_report(state.run_id)
    assert report["status"] == expected_status
    assert report["stop_reason"] == expected_reason
    assert report["completion_usage_totals"]["total_tokens"] == expected_total_tokens
    assert report["finalization_errors"] == []
    runtime_terminals = [
        message
        for message in agent.session["messages"]
        if (message.get("_pico_meta") or {}).get("origin") == "runtime_terminal"
    ]
    assert len(runtime_terminals) == runtime_terminal_count


@pytest.mark.parametrize(
    "error",
    [ValueError("provider bad"), OSError("provider io")],
)
def test_any_provider_exception_closes_model_error_and_reraises_original(
    tmp_path,
    error,
):
    provider = RaisingProvider(error)
    agent = build_native_agent(tmp_path, provider)

    with pytest.raises(type(error), match=str(error)):
        agent.ask("fail")

    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "model_error"
    assert agent.run_store.report_path(agent.current_task_state).exists()
    terminal = agent.session["messages"][-1]
    assert terminal["role"] == "assistant"
    assert terminal["_pico_meta"]["origin"] == "runtime_terminal"
    assert str(error) not in terminal["content"]


def test_preflight_exception_becomes_runtime_error(tmp_path, monkeypatch):
    agent = build_native_agent(tmp_path, NativeScriptProvider([]))
    monkeypatch.setattr(
        agent,
        "refresh_prefix",
        Mock(side_effect=ValueError("preflight")),
    )

    with pytest.raises(ValueError, match="preflight"):
        agent.ask("start")

    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "runtime_error"
    assert agent.run_store.report_path(agent.current_task_state).exists()


@pytest.mark.parametrize("fault_point", ["decode", "action_trace", "apply"])
def test_post_response_runtime_fault_preserves_primary_usage_and_terminalizes_once(
    tmp_path,
    monkeypatch,
    fault_point,
):
    primary = ValueError(fault_point)
    if fault_point == "apply":
        response = Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "tu_fault",
                "name": "read_file",
                "input": {"path": "README.md"},
            }],
            usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        )
    else:
        response = Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        )
    provider = NativeScriptProvider([response])
    agent = build_native_agent(tmp_path, provider)
    if fault_point == "decode":
        monkeypatch.setattr(agent_loop_module, "decode_action", Mock(side_effect=primary))
    elif fault_point == "action_trace":
        original_emit_trace = agent.emit_trace

        def fail_action_trace(task_state, event, payload=None):
            if event == "action_decoded":
                raise primary
            return original_emit_trace(task_state, event, payload)

        monkeypatch.setattr(agent, "emit_trace", fail_action_trace)
    else:
        monkeypatch.setattr(agent, "execute_tool", Mock(side_effect=primary))

    with pytest.raises(ValueError) as caught:
        agent.ask("exercise runtime fault")

    assert caught.value is primary
    assert len(provider.calls) == 1
    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "runtime_error"
    assert agent.current_task_state.attempts == 1
    assert agent.current_task_state.tool_steps == 0
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["completion_usage_totals"]["total_tokens"] == 3
    events = read_trace(agent)
    assert len([event for event in events if event["event"] == "run_finished"]) == 1
    assert len(
        [
            message
            for message in agent.session["messages"]
            if (message.get("_pico_meta") or {}).get("origin")
            == "runtime_terminal"
        ]
    ) == 1
    assert all(
        not (
            isinstance(message.get("content"), list)
            and message["content"]
            and message["content"][0].get("type") == "tool_use"
        )
        for message in agent.session["messages"]
    )


def test_build_failure_after_success_does_not_reuse_request_metadata(
    tmp_path,
    monkeypatch,
):
    agent = build_native_agent(
        tmp_path,
        NativeScriptProvider([
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "first"}],
                usage={},
            ),
        ]),
    )
    assert agent.ask("first run") == "first"
    assert agent.last_request_metadata["messages_count"] > 0
    monkeypatch.setattr(
        agent.context_manager,
        "build_request",
        Mock(side_effect=ValueError("build failed")),
    )

    with pytest.raises(ValueError, match="build failed"):
        agent.ask("second run")

    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert agent.last_request_metadata == {}
    assert report["last_request_metadata"] == {}


def test_keyboard_interrupt_closes_run_and_reraises(tmp_path):
    agent = build_native_agent(
        tmp_path,
        RaisingProvider(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        agent.ask("interrupt")

    assert agent.current_task_state.status == "stopped"
    assert agent.current_task_state.stop_reason == "interrupted"
    assert agent.run_store.report_path(agent.current_task_state).exists()
    assert agent.session["messages"][-1]["_pico_meta"]["origin"] == "runtime_terminal"


def test_finalizer_failure_does_not_mask_provider_exception(
    tmp_path,
    monkeypatch,
    caplog,
):
    secret = "github_pat_" + "F" * 32
    primary = ValueError("primary provider failure")
    agent = build_native_agent(tmp_path, RaisingProvider(primary))
    caplog.set_level(logging.DEBUG, logger="pico")
    monkeypatch.setattr(
        agent.run_store,
        "write_report",
        Mock(side_effect=OSError("report unavailable " + secret)),
    )

    with pytest.raises(ValueError, match="primary provider failure"):
        agent.ask("fail")

    assert agent.current_task_state.stop_reason == "model_error"
    events = read_trace(agent)
    assert any(event["event"] == "run_finished" for event in events)
    failure = next(
        event for event in events if event["event"] == "finalization_failed"
    )
    assert "report unavailable" in " ".join(failure["finalization_errors"])
    assert secret not in json.dumps(events) + caplog.text
    assert "OSError" in caplog.text


def test_provider_error_report_build_failure_does_not_mask_primary(tmp_path, monkeypatch):
    primary = ValueError("primary provider failure")
    agent = build_native_agent(tmp_path, RaisingProvider(primary))
    monkeypatch.setattr(
        agent,
        "build_report",
        Mock(side_effect=OSError("report build unavailable")),
    )

    with pytest.raises(ValueError, match="primary provider failure"):
        agent.ask("fail")

    assert agent.current_task_state.stop_reason == "model_error"
    failure = next(
        event for event in read_trace(agent) if event["event"] == "finalization_failed"
    )
    assert "report build unavailable" in " ".join(failure["finalization_errors"])
    assert len(" ".join(failure["finalization_errors"])) <= 300


def test_finalizer_failure_without_primary_is_raised(tmp_path, monkeypatch):
    agent = build_native_agent(
        tmp_path,
        NativeScriptProvider([
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "done"}],
                usage={},
            ),
        ]),
    )
    monkeypatch.setattr(
        agent.run_store,
        "write_report",
        Mock(side_effect=OSError("report unavailable")),
    )

    with pytest.raises(OSError, match="report unavailable"):
        agent.ask("finish")


def test_final_message_save_failure_is_persistence_error(tmp_path, monkeypatch):
    agent = build_native_agent(
        tmp_path,
        NativeScriptProvider([
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "done"}],
                usage={},
            ),
        ]),
    )
    original_save = agent.session_store.save

    def fail_assistant(session):
        if (
            session.get("messages")
            and session["messages"][-1].get("role") == "assistant"
        ):
            raise OSError("assistant save failed")
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", fail_assistant)

    with pytest.raises(OSError, match="assistant save failed"):
        agent.ask("finish")

    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "persistence_error"


def test_initial_user_save_failure_does_not_start_a_run(tmp_path, monkeypatch):
    agent = build_native_agent(tmp_path, NativeScriptProvider([]))
    monkeypatch.setattr(
        agent.session_store,
        "save",
        Mock(side_effect=OSError("user save failed")),
    )

    with pytest.raises(OSError, match="user save failed"):
        agent.ask("start")

    assert agent.current_task_state is None
    assert agent.session["messages"] == []
    assert not list((tmp_path / ".pico" / "runs").glob("run_*"))


@pytest.mark.parametrize("failure", ["start_run", "run_started"])
def test_run_start_artifact_failure_is_runtime_terminal(tmp_path, monkeypatch, failure):
    agent = build_native_agent(tmp_path, NativeScriptProvider([]))
    if failure == "start_run":
        monkeypatch.setattr(
            agent.run_store,
            "start_run",
            Mock(side_effect=OSError("run start failed")),
        )
        expected = "run start failed"
    else:
        original_emit_trace = agent.emit_trace

        def fail_run_started(task_state, event, payload=None):
            if event == "run_started":
                raise OSError("run trace failed")
            return original_emit_trace(task_state, event, payload)

        monkeypatch.setattr(agent, "emit_trace", fail_run_started)
        expected = "run trace failed"

    with pytest.raises(OSError, match=expected):
        agent.ask("start")

    assert agent.current_task_state.status == "failed"
    assert agent.current_task_state.stop_reason == "runtime_error"


def test_in_run_checkpoint_session_save_failure_is_persistence_error(
    tmp_path,
    monkeypatch,
):
    provider = NativeScriptProvider([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "tu_checkpoint",
                "name": "write_file",
                "input": {"path": "note.txt", "content": "saved\n"},
            }],
            usage={},
        ),
    ])
    agent = build_native_agent(tmp_path, provider)
    original_save = agent.session_store.save
    fault_injected = False
    failed_payload = None

    def fail_checkpoint_save(session):
        nonlocal fault_injected, failed_payload
        checkpoints = session.get("checkpoints") or {}
        current_id = checkpoints.get("current_id") or ""
        items = checkpoints.get("items") or {}
        current = items.get(current_id) or {}
        tool_use_ids = []
        tool_result_ids = []
        for message in session.get("messages") or []:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            tool_use_ids.extend(
                block.get("id")
                for block in content
                if block.get("type") == "tool_use"
            )
            tool_result_ids.extend(
                block.get("tool_use_id")
                for block in content
                if block.get("type") == "tool_result"
            )
        checkpoint_for_this_run = (
            current_id in items
            and current.get("current_goal") == "write"
            and current.get("summary", "").startswith("tool_executed:")
        )
        pair_present = (
            "tu_checkpoint" in tool_use_ids
            and "tu_checkpoint" in tool_result_ids
        )
        if not fault_injected and checkpoint_for_this_run and pair_present:
            fault_injected = True
            failed_payload = copy.deepcopy(session)
            raise OSError("checkpoint save failed")
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", fail_checkpoint_save)

    with pytest.raises(OSError, match="checkpoint save failed"):
        agent.ask("write")

    assert len(provider.calls) == 1
    assert fault_injected is True
    assert failed_payload["checkpoints"]["current_id"]
    assert failed_payload["checkpoints"]["current_id"] in failed_payload[
        "checkpoints"
    ]["items"]
    assert agent.current_task_state.stop_reason == "persistence_error"
    assert agent.current_task_state.status == "failed"


def test_rejected_tool_calls_do_not_consume_step_budget(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":1}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":1}}</tool>',
            "<final>done after rejected repeat</final>",
        ],
        max_steps=3,
    )

    answer = agent.ask("inspect README and finish")

    assert answer == "done after rejected repeat"
    assert agent.current_task_state.tool_steps == 2
    assert agent.current_task_state.stop_reason == "final_answer_returned"
    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    rejected = [
        event
        for event in trace_events
        if event.get("event") == "tool_executed" and event.get("tool_status") == "rejected"
    ]
    assert rejected
