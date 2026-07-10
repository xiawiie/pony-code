"""Regression test for the final-review Finding 1:

AgentLoop.run pre-appends the user turn via _append_user_turn, then
build_v2 must still ensure the injection-wrapped content is what
provider.complete_v2 actually receives — not the bare user string.
This end-to-end check sniffs the provider payload.
"""

import json

from pico.providers.response import Response, StopReason
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


class _SniffProvider:
    """v2 provider stub that records every complete_v2 call verbatim."""

    supports_prompt_cache = False
    supports_native_tools = True

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.last_completion_metadata = {}

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
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
    return Pico(
        model_client=provider,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


def test_provider_receives_injection_wrapped_user_message(tmp_path):
    """The last message the provider sees must contain <system-reminder>
    and <pico:workspace_state> — not the bare user string."""
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")

    provider = _SniffProvider(
        [Response(stop_reason=StopReason.END_TURN, content=[{"type": "text", "text": "done"}], usage={})]
    )
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    pico = Pico(model_client=provider, workspace=workspace, session_store=store, max_steps=3)

    pico.ask("what's in readme?")

    assert provider.calls, "provider was not called"
    call = provider.calls[0]
    last_msg = call["messages"][-1]
    last_content = last_msg["content"]
    # The provider must receive the injection-wrapped user turn, not the bare string.
    assert isinstance(last_content, str)
    assert "<system-reminder>" in last_content, (
        f"expected <system-reminder> in provider's last user message; got: {last_content[:200]!r}"
    )
    assert "<pico:workspace_state>" in last_content, (
        f"expected <pico:workspace_state> block; got: {last_content[:200]!r}"
    )
    assert "what's in readme?" in last_content


def test_message_count_invariant_after_injection(tmp_path):
    """build_v2 must NOT duplicate the user message when the loop pre-appended it."""
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")

    provider = _SniffProvider(
        [Response(stop_reason=StopReason.END_TURN, content=[{"type": "text", "text": "done"}], usage={})]
    )
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    pico = Pico(model_client=provider, workspace=workspace, session_store=store, max_steps=3)

    pico.ask("hi")

    # Session should carry exactly one user turn + one assistant turn (final).
    roles = [m["role"] for m in pico.session["messages"]]
    assert roles == ["user", "assistant"]
    # Provider saw the user turn wrapped, plus the (empty at call time) history.
    call = provider.calls[0]
    provider_roles = [m["role"] for m in call["messages"]]
    # Provider sees only the current user turn (nothing before it — fresh session).
    assert provider_roles == ["user"]


def test_one_snapshot_survives_retry_and_tool_step_while_feedback_is_one_shot(
    tmp_path,
    monkeypatch,
):
    render_calls = []

    def frozen_render(agent, user_message):
        render_calls.append(user_message)
        return (
            "<system-reminder><pico:memory_index>SNAPSHOT</pico:memory_index></system-reminder>\n"
            + user_message,
            {
                "intent": {"name": "default", "matched_keyword": "", "matched_reason": "test"},
                "injection_tokens": {"memory_index": 1},
                "injection_truncated": {},
                "injection_dropped": [],
                "injection_budget": 100,
            },
        )

    monkeypatch.setattr(
        "pico.agent_loop.render_current_user_message",
        frozen_render,
        raising=False,
    )
    provider = _SniffProvider([
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "<tool>{bad}</tool>"}],
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "README.md"}}],
            usage={"input_tokens": 2, "output_tokens": 1},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={"input_tokens": 3, "output_tokens": 1},
        ),
    ])
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
    assert all("SNAPSHOT" in content for content in sent)
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
