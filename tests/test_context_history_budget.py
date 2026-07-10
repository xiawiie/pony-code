"""Task A1: turn-based history budget drops old turn units atomically."""

from pico.context_manager import _drop_old_turns


def _msg(role, content, tool_use_id=None):
    m = {"role": role, "content": content, "_pico_meta": {}}
    if tool_use_id is not None:
        m["_pico_meta"]["tool_use_id"] = tool_use_id
    return m


def _flat_token_count(msg):
    # rough char-based estimate; each ascii char ~= 1 token in tests
    c = msg["content"]
    if isinstance(c, str):
        return len(c)
    total = 0
    for block in c:
        total += len(str(block.get("content", "")))
        total += len(str(block.get("input", "")))
    return total


def test_soft_cap_respected_when_exceeded():
    # 5 user messages of 100 chars each = 500 tokens
    msgs = [_msg("user", "x" * 100) for _ in range(5)]
    kept, dropped = _drop_old_turns(msgs, soft_cap_tokens=250, floor_count=1, token_of=_flat_token_count)
    assert dropped >= 2
    assert sum(_flat_token_count(m) for m in kept) <= 250 or len(kept) == 1


def test_floor_never_dropped():
    # 20 user messages, cap=0, floor=6 → keep last 6 even though over cap
    msgs = [_msg("user", f"m{i}") for i in range(20)]
    kept, dropped = _drop_old_turns(msgs, soft_cap_tokens=0, floor_count=6, token_of=_flat_token_count)
    assert len(kept) == 6
    assert dropped == 14
    assert kept[0]["content"] == "m14"
    assert kept[-1]["content"] == "m19"


def test_no_drop_when_under_cap():
    msgs = [_msg("user", "short") for _ in range(3)]
    kept, dropped = _drop_old_turns(msgs, soft_cap_tokens=10000, floor_count=6, token_of=_flat_token_count)
    assert kept == msgs
    assert dropped == 0


def test_multi_tool_use_turn_drop_atomicity():
    # A turn with 3 tool_use/tool_result pairs — all must drop together.
    msgs = [
        _msg("user", "old question 1"),
        _msg("assistant", [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}], tool_use_id="t1"),
        _msg("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "r1"}], tool_use_id="t1"),
        _msg("assistant", [{"type": "tool_use", "id": "t2", "name": "read_file", "input": {}}], tool_use_id="t2"),
        _msg("user", [{"type": "tool_result", "tool_use_id": "t2", "content": "r2"}], tool_use_id="t2"),
        _msg("assistant", [{"type": "tool_use", "id": "t3", "name": "read_file", "input": {}}], tool_use_id="t3"),
        _msg("user", [{"type": "tool_result", "tool_use_id": "t3", "content": "r3"}], tool_use_id="t3"),
        _msg("assistant", "final answer 1"),
        _msg("user", "new question 2"),
        _msg("assistant", "answer 2"),
    ]
    # Force dropping the first turn (10 messages up to the second user question)
    kept, dropped = _drop_old_turns(msgs, soft_cap_tokens=50, floor_count=2, token_of=_flat_token_count)
    # Every remaining assistant.tool_use.id must have a matching user.tool_result
    tool_use_ids = set()
    tool_result_ids = set()
    for m in kept:
        if isinstance(m["content"], list):
            for block in m["content"]:
                if block.get("type") == "tool_use":
                    tool_use_ids.add(block["id"])
                if block.get("type") == "tool_result":
                    tool_result_ids.add(block["tool_use_id"])
    assert tool_use_ids == tool_result_ids, "orphan tool_use or tool_result after drop"


def test_orphan_tool_use_never_produced():
    # If floor cuts through a tool_use pair, the algorithm must extend
    # the retention to keep the pair intact.
    msgs = [
        _msg("user", "q"),
        _msg("assistant", [{"type": "tool_use", "id": "t1", "name": "read", "input": {}}], tool_use_id="t1"),
        _msg("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}], tool_use_id="t1"),
        _msg("assistant", "done"),
    ]
    # floor=1 would naively cut mid-pair; algorithm must keep the tool_result reachable
    kept, _dropped = _drop_old_turns(msgs, soft_cap_tokens=0, floor_count=1, token_of=_flat_token_count)
    tool_use_ids = set()
    tool_result_ids = set()
    for m in kept:
        if isinstance(m["content"], list):
            for block in m["content"]:
                if block.get("type") == "tool_use":
                    tool_use_ids.add(block["id"])
                if block.get("type") == "tool_result":
                    tool_result_ids.add(block["tool_use_id"])
    assert tool_use_ids == tool_result_ids


def test_build_v2_drops_old_messages_when_cap_exceeded(tmp_path):
    from unittest.mock import MagicMock

    from pico.context.renderer import render_current_user_message
    from pico.context_manager import ContextManager

    a = MagicMock()
    a.prefix = "sys"
    a.tools = {}
    # Ten complete canonical turns followed by the current user turn.
    old_msgs = [
        message
        for index in range(10)
        for message in (
            _msg("user", f"old q {index} " + "y" * 200),
            _msg("assistant", "x" * 200),
        )
    ]
    a.session = {"messages": old_msgs + [_msg("user", "current q")]}
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(return_value="")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.context_config = {"history_soft_cap": 500, "history_floor_messages": 3}

    cm = ContextManager(a)
    snapshot, telemetry = render_current_user_message(a, "current q")
    request, metadata = cm.build_v2(
        injection_snapshot=snapshot,
        injection_telemetry=telemetry,
        preflight_metadata={},
    )
    assert metadata["dropped_messages"] > 0
    # Floor guarantees minimum tail; provider gets a bounded messages list.
    assert len(request["messages"]) < len(old_msgs) + 1
