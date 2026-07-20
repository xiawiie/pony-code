"""E2E: one Pony.ask exercises injection + digest + recall together."""

from pony.providers.response import Response, StopReason
from pony.runtime.application import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from pony.runtime.options import RuntimeOptions


class _SniffProvider:
    supports_prompt_cache = False

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.last_completion_metadata = {}

    def complete(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.calls.append({"messages": [dict(m) for m in messages]})
        return self.script.pop(0)


def test_full_turn_injects_recall_and_digests_large_tool_result(tmp_path):
    (tmp_path / "pony.toml").write_text(
        "[context.tool_results]\ninline_tokens = 100\ndigest_tokens = 128\n",
        encoding="utf-8",
    )
    # Seed a memory note that should match "cache".
    (tmp_path / ".pony" / "memory" / "notes").mkdir(parents=True)
    (tmp_path / ".pony" / "memory" / "notes" / "cache.md").write_text(
        "---\nname: cache\ntype: reference\ndescription: cache invariant\n---\nCache stays stable across turns.\n",
        encoding="utf-8",
    )
    # A big README so the bounded read still exceeds this test's 100-token cap.
    (tmp_path / "README.md").write_text("readme line\n" * 5000, encoding="utf-8")

    provider = _SniffProvider(
        [
        # Turn 1: model asks read_file.
        Response(
            stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_a",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    }
                ],
            usage={},
        ),
        # Turn 2: model returns final text after seeing the tool_result.
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "cache stays stable"}],
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
        options=RuntimeOptions(project_trusted=True, max_steps=3),
    )

    answer = pony.ask("上次讨论过 cache 的问题")
    assert "cache" in answer

    # 2 provider calls happened.
    assert len(provider.calls) == 2

    # Turn 1 provider input carries injection wrapping around user text.
    turn1_user_content = provider.calls[0]["messages"][-1]["content"]
    assert isinstance(turn1_user_content, str)
    assert (
        "<pony:workspace_state>" in turn1_user_content
        or "<system-reminder>" in turn1_user_content
    )
    # Recall block should be present (memory note matched "cache" keyword).
    assert "<pony:recalled_memory" in turn1_user_content
    assert "cache" in turn1_user_content.lower()

    # Turn 2: canonical messages contain the tool_result. Because the raw README exceeds
    # the token-based inline cap, content is the short [digest] rendering.
    turn2_msgs = provider.calls[1]["messages"]
    tool_result_msgs = [
        m
        for m in turn2_msgs
        if isinstance(m["content"], list)
        and any(b.get("type") == "tool_result" for b in m["content"])
    ]
    assert tool_result_msgs, "no tool_result in turn 2 canonical messages"
    tr_content = tool_result_msgs[-1]["content"][0]["content"]
    assert "[digest]" in tr_content, f"expected digest, got: {tr_content[:200]!r}"

    # No recall errors surfaced.
    assert pony.session.get("_recall_errors", {}).get("count", 0) == 0


def test_history_is_never_silently_dropped(tmp_path):
    """History below the request limit remains intact; compaction is the only exit."""
    provider = _SniffProvider(
        [
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
        options=RuntimeOptions(max_steps=3),
    )

    # Prime session with many messages BEFORE calling ask.
    for i in range(30):
        pony.session["messages"].append(
            {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"old-msg-{i} " + ("x" * 200),
            "_pony_meta": {"created_at": "t"},
            }
        )

    pony.ask("new question")

    call = provider.calls[0]
    assert pony.last_request_metadata.get("dropped_messages", 0) == 0
    serialized = repr(call["messages"])
    assert "old-msg-0" in serialized
    assert "old-msg-29" in serialized
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
