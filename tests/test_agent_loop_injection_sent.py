"""Regression test for the final-review Finding 1:

AgentLoop.run pre-appends the user turn via _append_user_turn, then
build_request must still ensure the injection-wrapped content is what
provider.complete actually receives — not the bare user string.
This end-to-end check sniffs the provider payload.
"""

import json

from pony.context.renderer import InjectionSnapshot, InjectionSource
from pony.providers.response import Response, StopReason
from pony.runtime.application import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from pony.runtime.options import RuntimeOptions


class _SniffProvider:
    """Structured provider stub that records every complete call verbatim."""

    supports_prompt_cache = False

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.last_completion_metadata = {}

    def complete(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.calls.append(
            {
                "system": system,
                "tools": tools,
                # Deep-enough copy: messages last content is what we assert on
                "messages": [dict(m) for m in messages],
                "cache_breakpoints": cache_breakpoints,
            }
        )
        return self.script.pop(0)


def build_native_agent(tmp_path, provider, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pony(
        model_client=provider,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True, **kwargs),
    )


def test_provider_receives_injection_wrapped_user_message(tmp_path):
    """The last message the provider sees must contain <system-reminder>
    and <pony:workspace_state> — not the bare user string."""
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")

    provider = _SniffProvider(
        [
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "done"}],
                usage={},
            )
        ]
    )
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    pony = Pony(
        model_client=provider,
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True, max_steps=3),
    )

    pony.ask("what's in readme?")

    assert provider.calls, "provider was not called"
    call = provider.calls[0]
    last_msg = call["messages"][-1]
    last_content = last_msg["content"]
    # The provider must receive the injection-wrapped user turn, not the bare string.
    assert isinstance(last_content, str)
    assert "<system-reminder>" in last_content, (
        f"expected <system-reminder> in provider's last user message; got: {last_content[:200]!r}"
    )
    assert "<pony:workspace_state>" in last_content, (
        f"expected <pony:workspace_state> block; got: {last_content[:200]!r}"
    )
    assert "what's in readme?" in last_content


def test_message_count_invariant_after_injection(tmp_path):
    """build_request must NOT duplicate the user message when the loop pre-appended it."""
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")

    provider = _SniffProvider(
        [
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "done"}],
                usage={},
            )
        ]
    )
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    pony = Pony(
        model_client=provider,
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True, max_steps=3),
    )

    pony.ask("hi")

    # Session should carry exactly one user turn + one assistant turn (final).
    roles = [m["role"] for m in pony.session["messages"]]
    assert roles == ["user", "assistant"]
    # Provider saw the user turn wrapped, plus the (empty at call time) history.
    call = provider.calls[0]
    provider_roles = [m["role"] for m in call["messages"]]
    # Provider sees only the current user turn (nothing before it — fresh session).
    assert provider_roles == ["user"]


def test_tool_created_summary_appears_next_top_level_turn_not_current_turn(tmp_path):
    provider = _SniffProvider(
        [
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    }
                ],
                usage={},
            ),
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "first done"}],
                usage={},
            ),
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "second done"}],
                usage={},
            ),
        ]
    )
    agent = build_native_agent(tmp_path, provider)

    assert agent.ask("read README") == "first done"
    assert agent.ask("what did README say?") == "second done"

    current_users = [
        next(
            message["content"]
            for message in reversed(call["messages"])
            if message["role"] == "user" and isinstance(message["content"], str)
        )
        for call in provider.calls
    ]
    assert "- README.md: 1: demo" not in current_users[0]
    assert "- README.md: 1: demo" not in current_users[1]
    assert "- README.md: 1: demo" in current_users[2]


def test_one_snapshot_survives_retry_and_tool_step_while_feedback_is_one_shot(
    tmp_path,
    monkeypatch,
):
    render_calls = []

    def frozen_snapshot(agent, user_message, runtime_feedback="", render_fn=None):
        del agent, runtime_feedback, render_fn
        render_calls.append(user_message)
        source = InjectionSource(
            name="memory_index",
            required=False,
            text=(
                "<system-reminder><pony:memory_index>"
                "SNAPSHOT</pony:memory_index></system-reminder>"
            ),
            token_count=1,
            status="included",
            reason_code="test",
            hard_cap=1_024,
            priority=2,
        )
        return InjectionSnapshot(
            current_user=user_message,
            runtime_feedback="",
            allocator_name="priority_allocator",
            sources=(source,),
        ), {
            "context_source_allocator": {"pool_tokens": 100},
            "injection_tokens": {"memory_index": 1},
            "injection_truncated": {},
            "injection_dropped": [],
            "injection_budget": 100,
        }

    monkeypatch.setattr(
        "pony.agent.loop.build_injection_snapshot",
        frozen_snapshot,
    )
    provider = _SniffProvider(
        [
        Response(
            stop_reason=StopReason.UNKNOWN,
            content=[{"type": "text", "text": "bad native response"}],
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
        Response(
            stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    }
                ],
            usage={"input_tokens": 2, "output_tokens": 1},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={"input_tokens": 3, "output_tokens": 1},
        ),
        ]
    )
    agent = build_native_agent(tmp_path, provider)
    assert agent.ask("inspect") == "done"
    assert render_calls == ["inspect"]

    sent = []
    for call in provider.calls:
        current = next(
            message["content"]
            for message in reversed(call["messages"])
            if message["role"] == "user" and isinstance(message["content"], str)
        )
        sent.append(current)
    exact_snapshot = (
        "<system-reminder><pony:memory_index>"
        "SNAPSHOT</pony:memory_index></system-reminder>\n\ninspect"
    )
    assert sent[0] == exact_snapshot
    assert all(exact_snapshot in content for content in sent)
    assert "runtime_feedback" not in sent[0]
    assert "runtime_feedback" in sent[1]
    assert "runtime_feedback" not in sent[2]
    assert len({call["system"][0]["text"] for call in provider.calls}) == 1

    canonical_text = json.dumps(agent.session["messages"])
    assert "SNAPSHOT" not in canonical_text
    assert "runtime_feedback" not in canonical_text

    events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    prompt_built_metadata = [
        event["request_metadata"]
        for event in events
        if event["event"] == "prompt_built"
    ]
    assert prompt_built_metadata
    assert all(
        "prompt_metadata" not in event
        for event in events
        if event["event"] == "prompt_built"
    )
    for event_name in ("model_requested", "action_decoded", "model_turn"):
        metadata = [
            event["request_metadata"]
            for event in events
            if event["event"] == event_name
        ]
        assert metadata == prompt_built_metadata


def test_retry_limit_feedback_is_one_shot_and_respects_attempt_cap(tmp_path):
    provider = _SniffProvider(
        [
        Response(
            stop_reason=StopReason.UNKNOWN,
                content=[{"type": "text", "text": f"bad-{index}"}],
            usage={
                "input_tokens": index + 1,
                "output_tokens": 1,
                "total_tokens": index + 2,
            },
        )
        for index in range(5)
        ]
    )
    agent = build_native_agent(tmp_path, provider, max_steps=1)

    answer = agent.ask("keep retrying")

    assert answer.startswith("Stopped after repeated malformed model responses")
    assert len(provider.calls) == 2
    assert agent.current_task_state.attempts == 2
    assert agent.current_task_state.tool_steps == 0
    assert agent.current_task_state.stop_reason == "retry_limit_reached"
    sent_users = [
        next(
            message["content"]
            for message in reversed(call["messages"])
            if message["role"] == "user" and isinstance(message["content"], str)
        )
        for call in provider.calls
    ]
    assert sent_users[0].count("<pony:runtime_feedback>") == 0
    assert sent_users[1].count("<pony:runtime_feedback>") == 1
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["model"]["attempts"] == 2
    assert report["tools"]["calls"] == 0
    assert report["run"]["stop_reason"] == "retry_limit_reached"
    assert report["model"]["usage"]["input_tokens"] == 3
    assert report["model"]["usage"]["output_tokens"] == 2
    assert report["model"]["usage"]["total_tokens"] == 5
    assert agent.session["messages"][-1]["content"] == answer
    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert (
        len([event for event in trace_events if event["event"] == "run_finished"]) == 1
    )
