"""Task 24: renderer wires recalled_memory source through recall_for_turn.

Verifies:
- recall block appears in the rendered current user message
- intent detection still works (recall keywords bump the recall budget)
- when no notes exist, recalled_memory silently drops (no crash)
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from pico.context.renderer import render_current_user_message
from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval


def _w(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_renderer_injects_recalled_memory(tmp_path):
    ws = tmp_path / "ws"
    _w(
        ws,
        "agent/cache.md",
        "---\nname: cache\ntype: reference\ndescription: cache note\n---\nCache is important.\n",
    )
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    a = SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": [], "messages": []},
        workspace=MagicMock(volatile_text=lambda: ""),
        repo_map=None,
        render_checkpoint_text=lambda: "",
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
    )
    text, tele = render_current_user_message(a, "上次讨论过 cache 的问题")
    assert "<pico:recalled_memory" in text
    assert "Cache is important." in text
    assert tele["intent"]["name"] == "recall"


def test_renderer_no_recall_when_store_empty(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    store = BlockStore(workspace_root=ws, user_root=tmp_path / "user")
    ret = Retrieval(store)
    a = SimpleNamespace(
        memory_store=store,
        memory_retrieval=ret,
        session={"recently_recalled": [], "messages": []},
        workspace=MagicMock(volatile_text=lambda: ""),
        repo_map=None,
        render_checkpoint_text=lambda: "",
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
    )
    text, _tele = render_current_user_message(a, "上次讨论过 cache")
    assert "<pico:recalled_memory" not in text
    assert "上次讨论过 cache" in text
