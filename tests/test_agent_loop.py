import copy
import json

import pytest

import pico.agent_loop as agent_loop_module
from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.agent_loop import AgentLoop
from pico.providers.response import Response, StopReason


class NativeScriptProvider:
    supports_prompt_cache = True
    supports_native_tools = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.last_completion_metadata = {"input_tokens": 999999}

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
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


def test_pico_ask_delegates_to_agent_loop(tmp_path):
    agent = build_agent(tmp_path, ["<final>Facade works.</final>"])

    assert agent.ask("Use facade") == "Facade works."


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
                usage={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
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
    assert [
        (message["role"], message["content"])
        for message in agent.session["messages"]
    ] == [("user", "write file")]
    assert agent.current_task_state.recovery_checkpoint_id


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


def test_terminal_paths_share_finish_run_helper(tmp_path, monkeypatch):
    calls = []
    original_finish = agent_loop_module._finish_run

    def spy_finish(**kwargs):
        calls.append(kwargs["trigger"])
        return original_finish(**kwargs)

    monkeypatch.setattr(agent_loop_module, "_finish_run", spy_finish)

    final_agent = build_agent(tmp_path / "final", ["<final>done</final>"])
    assert final_agent.ask("finish") == "done"

    limit_agent = build_agent(
        tmp_path / "limit",
        ['<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":1}}</tool>'],
        max_steps=1,
    )
    assert "step limit" in limit_agent.ask("hit limit")

    assert calls == ["run_finished", "step_limit_reached"]


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
