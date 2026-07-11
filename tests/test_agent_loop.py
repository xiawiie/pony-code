import copy
import json
import logging
from unittest.mock import Mock

import pytest

import pico.agent_loop as agent_loop_module
from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient
from pico.agent_loop import AgentLoop
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

    def complete(
        self,
        *,
        system,
        tools,
        messages,
        max_tokens,
        cache_breakpoints=None,
    ):
        raise self.error


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


def test_terminal_paths_share_finalizer(tmp_path, monkeypatch):
    calls = []
    original_finalize = agent_loop_module._finalize_run

    def spy_finalize(**kwargs):
        calls.append(kwargs["trigger"])
        return original_finalize(**kwargs)

    monkeypatch.setattr(agent_loop_module, "_finalize_run", spy_finalize)

    final_agent = build_agent(tmp_path / "final", ["<final>done</final>"])
    assert final_agent.ask("finish") == "done"

    limit_agent = build_agent(
        tmp_path / "limit",
        ['<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":1}}</tool>'],
        max_steps=1,
    )
    assert "step limit" in limit_agent.ask("hit limit")

    assert calls == ["run_finished", "step_limit_reached"]


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
    saves = 0

    def fail_checkpoint_save(session):
        nonlocal saves
        saves += 1
        if saves == 3:
            raise OSError("checkpoint save failed")
        return original_save(session)

    monkeypatch.setattr(agent.session_store, "save", fail_checkpoint_save)

    with pytest.raises(OSError, match="checkpoint save failed"):
        agent.ask("write")

    assert len(provider.calls) == 1
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
