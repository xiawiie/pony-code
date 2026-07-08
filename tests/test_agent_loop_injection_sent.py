"""Regression test for the final-review Finding 1:

AgentLoop.run pre-appends the user turn via _append_user_turn, then
build_v2 must still ensure the injection-wrapped content is what
provider.complete_v2 actually receives — not the bare user string.
This end-to-end check sniffs the provider payload.
"""

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
