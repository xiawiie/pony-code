# tests/test_message_invariants.py
"""Task E9: property-style invariants on the message array shape."""

from unittest.mock import MagicMock

from pico.context.renderer import render_current_user_message
from pico.context_manager import ContextManager
from pico.messages import strip_pico_meta


def _make_agent_with_messages(messages):
    a = MagicMock()
    a.prefix = "sys"
    a.tools = {}
    a.session = {"messages": messages, "recently_recalled": []}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.context_config = {}
    return a


def test_message_immutability_across_turns():
    """build_request must not mutate session["messages"] entries."""
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    snapshot_before = [dict(m) for m in msgs]
    agent = _make_agent_with_messages(msgs)
    cm = ContextManager(agent)
    for user_message in ("q2", "q3"):
        agent.session["messages"].append(
            {
                "role": "user",
                "content": user_message,
                "_pico_meta": {"created_at": "2026-07-10T00:00:00+00:00"},
            }
        )
        snapshot, telemetry = render_current_user_message(agent, user_message)
        cm.build_request(
            injection_snapshot=snapshot,
            injection_telemetry=telemetry,
            preflight_metadata={},
        )
        agent.session["messages"].append(
            {"role": "assistant", "content": "ack", "_pico_meta": {}}
        )
    # Session's original entries should be byte-identical after 2 builds.
    assert agent.session["messages"][0] == snapshot_before[0]
    assert agent.session["messages"][1] == snapshot_before[1]


def test_pico_meta_never_in_provider_payload():
    """strip_pico_meta ensures no _pico_meta reaches the provider."""
    src = [
        {"role": "user", "content": "hi", "_pico_meta": {"a": 1}},
        {"role": "assistant", "content": "yo", "_pico_meta": {"b": 2}},
    ]
    cleaned = strip_pico_meta(src)
    for m in cleaned:
        assert "_pico_meta" not in m


def test_recently_recalled_deque_bounded(tmp_path):
    """After N recall_for_turn calls, session["recently_recalled"] must
    stay bounded to skip_recent_turns + 1."""
    from types import SimpleNamespace
    from pico.memory.block_store import BlockStore
    from pico.memory.recall import recall_for_turn
    from pico.memory.retrieval import Retrieval

    (tmp_path / "agent").mkdir(parents=True)
    (tmp_path / "agent" / "cache.md").write_text(
        "---\nname: cache\ntype: feedback\ndescription: cache\n---\np1\n", encoding="utf-8"
    )
    store = BlockStore(workspace_root=tmp_path, user_root=tmp_path / "user")
    ret = Retrieval(store)

    a = SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": []},
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={"recall": {"min_score": 0.01, "top_k": 2, "max_tokens_per_note": 400, "skip_recent_turns": 2}},
    )
    for _ in range(10):
        recall_for_turn(a, "cache", budget_tokens=1000)
    # skip_recent_turns=2 → deque bounded to at most 3 entries.
    assert len(a.session["recently_recalled"]) <= 3
