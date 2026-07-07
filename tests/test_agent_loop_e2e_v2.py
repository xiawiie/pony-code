"""端到端：AgentLoop 用 v2 provider + v2 messages 完成一轮 tool_use → tool_result → final."""

from pico.providers.response import Response, StopReason


class _StubProviderV2:
    """按顺序返回 canned responses."""
    supports_prompt_cache = False
    supports_native_tools = True

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.last_completion_metadata = {}

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.calls.append({
            "system": system,
            "tools": tools,
            "messages": list(messages),
            "cache_breakpoints": cache_breakpoints,
        })
        return self.script.pop(0)


def test_end_to_end_tool_call_then_final(tmp_path):
    from pico.runtime import Pico
    from pico.session_store import SessionStore
    from pico.workspace import WorkspaceContext

    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    provider = _StubProviderV2([
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "toolu_a",
                "name": "read_file",
                "input": {"path": "README.md", "start": 1, "end": 1},
            }],
            usage={},
        ),
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "done"}],
            usage={},
        ),
    ])

    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    pico = Pico(
        model_client=provider,
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=3,
    )

    result = pico.ask("what's in readme?")
    assert result.strip() == "done"

    msgs = pico.session["messages"]
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


def test_end_to_end_wraps_non_v2_provider_with_fallback(tmp_path):
    """FakeModelClient (no complete_v2) must be wrapped in FallbackAdapter."""
    from pico import FakeModelClient
    from pico.providers.fallback_adapter import FallbackAdapter
    from pico.runtime import Pico
    from pico.session_store import SessionStore
    from pico.workspace import WorkspaceContext

    inner = FakeModelClient(["<final>ok</final>"])
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    pico = Pico(
        model_client=inner,
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )

    assert isinstance(pico.model_client, FallbackAdapter)
    assert pico.ask("hi") == "ok"


def test_end_to_end_v2_provider_not_double_wrapped(tmp_path):
    """A provider that already has complete_v2 stays as-is."""
    from pico.providers.fallback_adapter import FallbackAdapter
    from pico.runtime import Pico
    from pico.session_store import SessionStore
    from pico.workspace import WorkspaceContext

    provider = _StubProviderV2([
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "hi"}],
            usage={},
        ),
    ])
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    pico = Pico(
        model_client=provider,
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )

    assert pico.model_client is provider
    assert not isinstance(pico.model_client, FallbackAdapter)
    assert pico.ask("hi") == "hi"
