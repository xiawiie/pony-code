"""P1 (message-paradigm migration) smoke test.

Locks in the Phase 1 Definition of Done: the symbols that constitute the
new provider/context/agent-loop surface must exist and expose their v2
entry points.

- Anthropic adapter has `complete_v2`
- FallbackAdapter wraps non-tool_use backends via `complete_v2`
- Session store carries the v1/v2→v3 migrator helper
- ContextManager exposes `build_v2`
- agent_loop.py exports copy-on-write message helpers
"""


def test_p1_smoke_all_checkpoints_reachable():
    from pico.providers.response import Response, StopReason  # noqa: F401
    from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient
    from pico.providers.fallback_adapter import FallbackAdapter
    from pico.session_store import SessionStore, migrate_session_to_v3  # noqa: F401
    from pico.context_manager import ContextManager
    from pico.agent_loop import (
        SessionCommitError,
        _commit_session,
        _plain_message,
        _prepare_tool_result,
    )

    assert hasattr(AnthropicCompatibleModelClient, "complete_v2")
    assert hasattr(FallbackAdapter, "complete_v2")
    assert hasattr(ContextManager, "build_v2")
    assert callable(_commit_session)
    assert callable(_plain_message)
    assert callable(_prepare_tool_result)
    assert callable(migrate_session_to_v3)
    assert issubclass(SessionCommitError, RuntimeError)
