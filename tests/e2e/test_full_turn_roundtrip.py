"""E2E: one Pico.ask exercises injection + digest + recall together."""

from pico.providers.response import Response, StopReason
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


class _SniffProvider:
    supports_prompt_cache = False
    supports_native_tools = True

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.last_completion_metadata = {}

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.calls.append({"messages": [dict(m) for m in messages]})
        return self.script.pop(0)


def test_full_turn_injects_recall_and_digests_large_tool_result(tmp_path):
    # Seed a memory note that should match "cache".
    (tmp_path / ".pico" / "memory" / "agent").mkdir(parents=True)
    (tmp_path / ".pico" / "memory" / "agent" / "cache.md").write_text(
        "---\nname: cache\ntype: reference\ndescription: cache invariant\n---\nCache stays stable across turns.\n",
        encoding="utf-8",
    )
    # A big README so read_file returns > 1200 chars.
    (tmp_path / "README.md").write_text("readme line\n" * 500, encoding="utf-8")

    provider = _SniffProvider([
        # Turn 1: model asks read_file.
        Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{"type": "tool_use", "id": "toolu_a", "name": "read_file", "input": {"path": "README.md"}}],
            usage={},
        ),
        # Turn 2: model returns final text after seeing the tool_result.
        Response(stop_reason=StopReason.END_TURN, content=[{"type": "text", "text": "cache stays stable"}], usage={}),
    ])
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    pico = Pico(model_client=provider, workspace=workspace, session_store=store, max_steps=3)

    answer = pico.ask("上次讨论过 cache 的问题")
    assert "cache" in answer

    # 2 provider calls happened.
    assert len(provider.calls) == 2

    # Turn 1 provider input carries injection wrapping around user text.
    turn1_user_content = provider.calls[0]["messages"][-1]["content"]
    assert isinstance(turn1_user_content, str)
    assert "<pico:workspace_state>" in turn1_user_content or "<system-reminder>" in turn1_user_content
    # Recall block should be present (memory note matched "cache" keyword).
    assert "<pico:recalled_memory" in turn1_user_content
    assert "cache" in turn1_user_content.lower()

    # Turn 2: canonical messages contain the tool_result. Because the raw README > 1200
    # chars, digest_applied=True → content is the short [digest] rendering.
    turn2_msgs = provider.calls[1]["messages"]
    tool_result_msgs = [
        m for m in turn2_msgs
        if isinstance(m["content"], list)
        and any(b.get("type") == "tool_result" for b in m["content"])
    ]
    assert tool_result_msgs, "no tool_result in turn 2 canonical messages"
    tr_content = tool_result_msgs[-1]["content"][0]["content"]
    assert "[digest]" in tr_content, f"expected digest, got: {tr_content[:200]!r}"

    # No recall errors surfaced.
    assert pico.session.get("_recall_errors", {}).get("count", 0) == 0


def test_history_budget_triggers_drop(tmp_path):
    """A session with many pre-existing messages + a tight soft_cap should drop old turns."""
    # Populate pico.toml so context_config picks up a tight soft_cap.
    (tmp_path / "pico.toml").write_text(
        "[context]\nhistory_soft_cap = 500\nhistory_floor_messages = 4\n",
        encoding="utf-8",
    )

    provider = _SniffProvider([
        Response(stop_reason=StopReason.END_TURN, content=[{"type": "text", "text": "done"}], usage={}),
    ])
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    pico = Pico(model_client=provider, workspace=workspace, session_store=store, max_steps=3)

    # Sanity: pico.toml overrides actually reached context_config.
    assert pico.context_config["history_soft_cap"] == 500
    assert pico.context_config["history_floor_messages"] == 4

    # Prime session with many messages BEFORE calling ask.
    for i in range(30):
        pico.session["messages"].append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"old-msg-{i} " + ("x" * 200),
            "_pico_meta": {"created_at": "t"},
        })

    pico.ask("new question")

    call = provider.calls[0]
    metadata_dropped = pico.last_request_metadata.get("dropped_messages", 0)
    assert metadata_dropped > 0, "expected some messages to be dropped under tight cap"
    # Floor honored: last N ≥ 4 messages preserved.
    assert len(call["messages"]) >= 4
    # No orphan tool_use blocks (there were none seeded; this is a smoke).
    tool_use_ids = set()
    tool_result_ids = set()
    for m in call["messages"]:
        if isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    tool_use_ids.add(b["id"])
                if b.get("type") == "tool_result":
                    tool_result_ids.add(b["tool_use_id"])
    assert tool_use_ids == tool_result_ids
