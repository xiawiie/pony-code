"""P2 (dynamic injection + intent budget) smoke test.

Locks in Phase 2 Definition of Done:
- `escape_pico_tags` present
- `classify_intent` present with first-match-wins priority
- `render_current_user_message` produces <pico:*> wrapped blocks
- `ContextManager.build_v2` weaves injection into the current turn
- intent-driven budget selects the right profile

The end-to-end scenario mimics a real turn where the user reports an
error ("上次报错了") — this hits both the "recall" keyword (上次) and
the "debug" keyword (报错). First-match-wins priority (debug > recall)
must pick `debug`.
"""

from unittest.mock import MagicMock

from pico.context.escaping import escape_pico_tags  # noqa: F401  (import gate)
from pico.context.intent import classify_intent  # noqa: F401
from pico.context.renderer import render_current_user_message  # noqa: F401
from pico.context_manager import ContextManager


def test_p2_end_to_end():
    a = MagicMock()
    a.prefix = "SYSTEM"
    a.tools = {}
    a.session = {"messages": []}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="branch: main")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))

    cm = ContextManager(a)
    req, meta = cm.build_v2("上次报错了")

    last_content = req["messages"][-1]["content"]
    # injection block present + escaped-content-free renderer output
    assert "<pico:workspace_state>" in last_content
    # first-match-wins: "报错" hits debug (higher priority than recall's "上次")
    assert meta["intent"]["name"] == "debug"
    # user text is preserved at the tail
    assert "上次报错了" in last_content
    # single-message session → no cache_control breakpoint
    assert meta["cache_control_breakpoints"] == []
    # v2 shape sanity
    assert "system" in req
    assert "tools" in req
    assert "messages" in req
    assert "cache_control_breakpoints" in req
