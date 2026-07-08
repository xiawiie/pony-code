"""P3 (memory structuring + recall + digest) smoke test.

End-to-end: create a per-topic memory file with frontmatter, ask a
question that matches its description, verify the renderer surfaces
`<pico:recalled_memory>` in the injection block, and confirm the
digest path is wired for large tool_result payloads.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from pico.context.digest import digest_tool_result, should_digest
from pico.context.renderer import render_current_user_message
from pico.memory.block_store import BlockStore
from pico.memory.retrieval import Retrieval


def test_p3_end_to_end(tmp_path):
    ws = tmp_path / "ws"
    (ws / "agent").mkdir(parents=True)
    (ws / "agent" / "cache.md").write_text(
        "---\nname: cache\ntype: reference\ndescription: cache invariant\n---\nCache stability rules.\n",
        encoding="utf-8",
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

    text, tele = render_current_user_message(a, "上次讨论过 cache 的事")
    assert "<pico:recalled_memory" in text
    assert "Cache stability rules." in text
    assert tele["intent"]["name"] == "recall"

    long_result = "line\n" * 500
    assert should_digest(long_result)
    d = digest_tool_result("read_file", {"path": "a.py"}, long_result, raw_path="raw/x.txt")
    assert d.source_hash
    assert "a.py" in d.title
