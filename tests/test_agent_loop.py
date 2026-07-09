import json

import pytest

import pico.agent_loop as agent_loop_module
from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.agent_loop import AgentLoop
from pico.providers.response import Response, StopReason


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


class _NativeScriptProvider:
    supports_prompt_cache = False
    supports_native_tools = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.last_completion_metadata = {}

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        if not self.responses:
            raise RuntimeError("native script exhausted")
        return self.responses.pop(0)


def build_native_agent(tmp_path, responses, max_steps=6):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=_NativeScriptProvider(responses),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=max_steps,
    )


def trace_events(agent):
    return [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]


def test_agent_loop_records_action_metadata_for_final(tmp_path):
    agent = build_native_agent(
        tmp_path,
        [
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "Plain final."}],
                usage={},
            ),
        ],
    )

    answer = agent.ask("Return a final answer")

    assert answer == "Plain final."
    events = trace_events(agent)
    model_parsed = [event for event in events if event["event"] == "model_parsed"][-1]
    model_turn = [event for event in events if event["event"] == "model_turn"][-1]
    assert model_parsed["action_type"] == "final"
    assert model_parsed["action_origin"] == "plain_text_final"
    assert model_parsed["ignored_tool_count"] == 0
    assert model_turn["action_type"] == "final"
    assert model_turn["action_origin"] == "plain_text_final"
    assert model_turn["ignored_tool_count"] == 0


def test_agent_loop_executes_only_first_native_tool_and_records_ignored_count(tmp_path):
    agent = build_native_agent(
        tmp_path,
        [
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_first",
                        "name": "read_file",
                        "input": {"path": "README.md", "start": 1, "end": 1},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_ignored",
                        "name": "read_file",
                        "input": {"path": "ignored.txt", "start": 1, "end": 1},
                    },
                ],
                usage={},
            ),
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "Finished."}],
                usage={},
            ),
        ],
    )

    answer = agent.ask("Read the first file")

    assert answer == "Finished."
    tool_records = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert len(tool_records) == 1
    assert tool_records[0]["name"] == "read_file"
    assert tool_records[0]["args"]["path"] == "README.md"
    notices = [
        item
        for item in agent.session["history"]
        if item["role"] == "runtime" and item.get("ignored_tool_count") == 1
    ]
    assert notices
    events = trace_events(agent)
    model_parsed = [event for event in events if event["event"] == "model_parsed"][0]
    model_turn = [event for event in events if event["event"] == "model_turn"][0]
    assert model_parsed["action_type"] == "tool"
    assert model_parsed["action_origin"] == "native_tool_use"
    assert model_parsed["ignored_tool_count"] == 1
    assert model_turn["action_type"] == "tool"
    assert model_turn["action_origin"] == "native_tool_use"
    assert model_turn["ignored_tool_count"] == 1


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


def test_malformed_tool_retry_is_visible_to_next_model_request(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")

    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    agent = Pico(
        model_client=FakeModelClient([
            "<tool>{not valid json</tool>",
            "<final>Recovered.</final>",
        ]),
        workspace=workspace,
        session_store=store,
    )

    assert agent.ask("do it") == "Recovered."

    second_prompt = agent.model_client.prompts[1]
    assert "<pico:runtime_feedback>" in second_prompt
    assert "valid <tool> call" in second_prompt
    assert agent.session["runtime_feedback"]["next_model_visible_notice"] == ""


def test_malformed_tool_retry_after_tool_result_tail_is_visible_to_next_request(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")

    class _SniffingNativeProvider:
        supports_prompt_cache = False
        supports_native_tools = True

        def __init__(self):
            self.calls = []
            self.last_completion_metadata = {}
            self.responses = [
                Response(
                    stop_reason=StopReason.TOOL_USE,
                    content=[{
                        "type": "tool_use",
                        "id": "toolu_read",
                        "name": "read_file",
                        "input": {"path": "README.md", "start": 1, "end": 1},
                    }],
                    usage={},
                ),
                Response(
                    stop_reason=StopReason.END_TURN,
                    content=[{"type": "text", "text": "<tool>{not valid json</tool>"}],
                    usage={},
                ),
                Response(
                    stop_reason=StopReason.END_TURN,
                    content=[{"type": "text", "text": "<final>Recovered.</final>"}],
                    usage={},
                ),
            ]

        def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
            self.calls.append({"messages": [dict(message) for message in messages]})
            return self.responses.pop(0)

    provider = _SniffingNativeProvider()
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    agent = Pico(model_client=provider, workspace=workspace, session_store=store, approval_policy="auto")

    assert agent.ask("read then continue") == "Recovered."

    third_request_messages = provider.calls[2]["messages"]
    third_request_text = "\n".join(
        block.get("text", "")
        for message in third_request_messages
        for block in (
            message["content"]
            if isinstance(message["content"], list)
            else [{"type": "text", "text": message["content"]}]
        )
        if block.get("type") == "text"
    )
    assert "<pico:runtime_feedback>" in third_request_text
    assert "valid <tool> call" in third_request_text
    assert agent.session["runtime_feedback"]["next_model_visible_notice"] == ""


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
