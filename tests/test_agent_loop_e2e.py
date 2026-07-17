"""端到端：AgentLoop 使用 structured request 完成一轮 tool_use → tool_result → final."""

from pony.providers.response import Response, StopReason
from pony.runtime.options import RuntimeOptions


class _StubProvider:
    """按顺序返回 canned responses."""

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
            "messages": list(messages),
            "cache_breakpoints": cache_breakpoints,
            }
        )
        return self.script.pop(0)


def test_end_to_end_tool_call_then_final(tmp_path):
    from pony.runtime.application import Pony
    from pony.state.session_store import SessionStore
    from pony.workspace.context import WorkspaceContext

    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    provider = _StubProvider(
        [
        Response(
            stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                "type": "tool_use",
                "id": "toolu_a",
                "name": "read_file",
                "input": {"path": "README.md", "start": 1, "end": 1},
                    }
                ],
            usage={},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={},
        ),
        ]
    )

    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    pony = Pony(
        model_client=provider,
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(approval_policy="auto", max_steps=3),
    )

    result = pony.ask("what's in readme?")
    assert result.strip() == "done"

    msgs = pony.session["messages"]
    # 应该有：user + assistant(tool_use) + user(tool_result) + assistant("done")
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert isinstance(msgs[1]["content"], list)
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert msgs[1]["content"][0]["name"] == "read_file"
    assert isinstance(msgs[2]["content"], list)
    assert msgs[2]["content"][0]["type"] == "tool_result"
    assert msgs[2]["content"][0]["tool_use_id"] == "toolu_a"
    # Final assistant turn is plain string
    assert msgs[3]["content"] == "done"

    # Task E7: assert the provider actually saw the injection wrapping.
    turn1_user_content = provider.calls[0]["messages"][-1]["content"]
    assert isinstance(turn1_user_content, str)
    assert "<system-reminder>" in turn1_user_content


def test_end_to_end_fake_provider_uses_structured_surface(tmp_path):
    from benchmarks.support.fake_provider import FakeModelClient
    from pony.runtime.application import Pony
    from pony.state.session_store import SessionStore
    from pony.workspace.context import WorkspaceContext

    inner = FakeModelClient(["ok"])
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    pony = Pony(
        model_client=inner,
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(approval_policy="auto"),
    )

    assert pony.model_client is inner
    assert pony.ask("hi") == "ok"


def test_end_to_end_structured_provider_stays_as_is(tmp_path):
    from pony.runtime.application import Pony
    from pony.state.session_store import SessionStore
    from pony.workspace.context import WorkspaceContext

    provider = _StubProvider(
        [
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "hi"}],
            usage={},
        ),
        ]
    )
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    pony = Pony(
        model_client=provider,
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(approval_policy="auto"),
    )

    assert pony.model_client is provider
    assert pony.ask("hi") == "hi"
