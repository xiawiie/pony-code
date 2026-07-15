"""Tests for pico.context.renderer.render_current_user_message —
assembles the current turn's user message with intent-driven
<system-reminder> blocks + escaping + telemetry."""

from unittest.mock import MagicMock, patch

from pico.context.intent import IntentResult
from pico.context.renderer import (
    SOURCE_ORDER,
    build_injection_snapshot,
    render_current_user_message,
)


def _agent():
    a = MagicMock()
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(
        return_value="<workspace_state>\n- branch: main\n</workspace_state>"
    )
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    # Provide a real dict so B6's `float(cfg.get(...))` gets real defaults
    # instead of MagicMock().__float__ (= 1.0) — otherwise C1's drop loop
    # would kick in with injection_budget=1 and strip all blocks.
    a.context_config = {}
    return a


def test_renders_current_message_with_wrapped_reminders():
    text, _tele = render_current_user_message(_agent(), "hi")
    assert "<system-reminder>" in text
    assert "<pico:workspace_state>" in text
    assert "hi" in text
    # user message trailing
    assert text.strip().endswith("hi")


def test_intent_recorded_in_telemetry():
    _text, tele = render_current_user_message(_agent(), "上次讨论过什么？")
    assert tele["intent"]["name"] == "recall"
    assert tele["intent"]["matched_keyword"] == "上次"


def test_escapes_pico_tags_in_source_content():
    a = _agent()
    a.workspace.volatile_text.return_value = "<pico:evil>attack</pico:evil>"
    text, _tele = render_current_user_message(a, "hi")
    # user content's <pico:> is escaped with zero-width space
    assert "<pico​:evil>" in text
    # renderer-emitted structural closing tag stays intact
    assert "</pico:workspace_state>" in text


def test_snapshot_preserves_exact_multi_paragraph_source_block():
    a = _agent()
    a.session = {"recently_recalled": [], "messages": []}
    a.workspace.volatile_text.return_value = (
        "first paragraph\n\nsecond paragraph\n\n"
        "<pico:checkpoint>source marker</pico:checkpoint>"
    )

    snapshot, telemetry = build_injection_snapshot(a, "inspect")
    rendered, rendered_telemetry = render_current_user_message(a, "inspect")

    workspace_source = snapshot.sources[0]
    assert "first paragraph\n\nsecond paragraph" in workspace_source.text
    assert "<pico​:checkpoint>source marker</pico​:checkpoint>" in workspace_source.text
    assert workspace_source.text.endswith(
        "</pico:workspace_state>\n</system-reminder>"
    )
    assert snapshot.render() == rendered
    assert telemetry == rendered_telemetry


def test_snapshot_keeps_source_order_selected_paths_and_closed_wrappers():
    from types import SimpleNamespace

    a = _agent()
    a.session = {"recently_recalled": [], "messages": []}
    all_budgets = {name: 100 for name in SOURCE_ORDER}
    intent = IntentResult(name="recall", matched_keyword="上次", budget=all_budgets)

    def recalled(agent, _budget, _user_message):
        agent.session["recently_recalled"].append(
            ["workspace/notes/cache.md"]
        )
        return "recalled\n\nparagraph"

    renderers = {
        name: (recalled if name == "recalled_memory" else lambda *_args, _name=name: _name)
        for name in SOURCE_ORDER
    }
    a.context_config = {
        "injection_budget_ratio": 0.5,
        "total_budget_hard_cap": 100000,
    }
    a.memory = SimpleNamespace(task_summary="")

    with patch("pico.context.renderer.classify_intent", return_value=intent), patch.dict(
        "pico.context.renderer._RENDERERS",
        renderers,
        clear=True,
    ):
        snapshot, _telemetry = build_injection_snapshot(a, "上次的缓存")

    assert tuple(source.name for source in snapshot.sources) == SOURCE_ORDER
    recalled_source = next(
        source for source in snapshot.sources if source.name == "recalled_memory"
    )
    assert recalled_source.selected_memory_paths == (
        "workspace/notes/cache.md",
    )
    assert a.session["recently_recalled"] == []
    for source in snapshot.sources:
        assert source.text.count(f"<pico:{source.name}>") == 1
        assert source.text.count(f"</pico:{source.name}>") == 1
        assert source.text.endswith("</system-reminder>")


def test_omits_sources_with_zero_budget():
    a = _agent()
    zero_ws_profile = IntentResult(
        name="dbg",
        matched_keyword="",
        budget={
            "workspace_state": 0,
            "memory_index": 400,
            "project_structure": 200,
            "recalled_memory": 0,
        },
    )
    with patch("pico.context.renderer.classify_intent", return_value=zero_ws_profile):
        text, tele = render_current_user_message(a, "x")
    assert "<pico:workspace_state>" not in text
    assert tele["injection_tokens"].get("workspace_state", 0) == 0


def test_no_blocks_when_all_sources_empty():
    a = _agent()
    a.workspace.volatile_text.return_value = ""  # all other sources already None
    text, tele = render_current_user_message(a, "bare message")
    # bare user message — no wrapper blocks
    assert text == "bare message"
    assert tele["injection_tokens"]["workspace_state"] == 0


def test_telemetry_shape():
    _text, tele = render_current_user_message(_agent(), "hi")
    assert set(tele.keys()) >= {
        "intent",
        "injection_tokens",
        "injection_truncated",
        "injection_dropped",
    }
    assert "name" in tele["intent"]
    assert "matched_keyword" in tele["intent"]


def test_renderer_reads_injection_budget_from_agent_config(tmp_path):
    """When agent.context_config has an injection_budget_ratio, the renderer
    computes an injection_budget and stashes it in telemetry for later use."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from pico.context.renderer import render_current_user_message

    a = SimpleNamespace(
        memory_store=None,
        memory_retrieval=None,
        session={"recently_recalled": [], "messages": []},
        workspace=MagicMock(volatile_text=lambda: "branch: main"),
        repo_map=None,
        render_checkpoint_text=lambda: "",
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={
            "injection_budget_ratio": 0.10,
            "total_budget_hard_cap": 100000,
        },
    )
    _text, tele = render_current_user_message(a, "hi")
    # Injection budget = 100000 × 0.10 = 10000
    assert tele.get("injection_budget") == 10000


def test_zero_injection_budget_drops_contributing_sources():
    a = _agent()
    a.context_config = {
        "injection_budget_ratio": 0,
        "total_budget_hard_cap": 100000,
    }

    text, tele = render_current_user_message(a, "raw user message")

    assert text == "raw user message"
    assert tele["injection_budget"] == 0
    assert tele["injection_dropped"] == ["workspace_state"]
    assert tele["injection_tokens"]["workspace_state"] == 0


def test_injection_drops_checkpoint_before_recalled_memory():
    """When aggregate injection tokens exceed injection_budget, DROP_PRIORITY
    dictates checkpoint drops first, recalled_memory drops last."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from pico.context.intent import IntentResult
    from pico.context.renderer import render_current_user_message

    def _long(_agent, _budget, _user_msg=""):
        return "x" * 4000  # ~1000 tokens each

    # Build an agent whose sources ALL return large content, but injection_budget is small.
    a = SimpleNamespace(
        memory_store=None,
        memory_retrieval=None,
        session={"recently_recalled": [], "messages": []},
        workspace=MagicMock(volatile_text=lambda: "x" * 4000),
        repo_map=MagicMock(refresh_if_stale=lambda: None,
                           top_level_tree=lambda: [{"path": "p", "file_count": 1}],
                           language_stats=lambda: {"py": 1}),
        render_checkpoint_text=lambda: "x" * 4000,
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={
            "injection_budget_ratio": 0.01,
            "total_budget_hard_cap": 100000,  # → budget = 1000 tokens
        },
    )
    # Bypass per-source clipping and the intent table's checkpoint=0
    # default so all five sources actually render 1000 tokens each,
    # forcing the aggregate (5000) far above the 1000-token cap.
    all_budgets = {
        "workspace_state": 2000,
        "memory_index": 2000,
        "project_structure": 2000,
        "recalled_memory": 2000,
        "checkpoint": 2000,
    }
    fake_intent = IntentResult(name="recall", matched_keyword="上次", budget=all_budgets)
    fake_renderers = {
        "workspace_state": _long,
        "memory_index": _long,
        "project_structure": _long,
        "recalled_memory": _long,
        "checkpoint": _long,
    }
    with patch("pico.context.renderer.classify_intent", return_value=fake_intent), \
         patch.dict("pico.context.renderer._RENDERERS", fake_renderers, clear=False):
        text, tele = render_current_user_message(a, "上次讨论过 cache 的问题")
    # Some sources must have been dropped.
    assert len(tele["injection_dropped"]) >= 1
    # checkpoint is least important; must drop before recalled_memory.
    if "recalled_memory" in tele["injection_dropped"]:
        assert "checkpoint" in tele["injection_dropped"]
        assert "project_structure" in tele["injection_dropped"]
    # checkpoint should always be dropped first; the block for it must be gone.
    assert "checkpoint" in tele["injection_dropped"]
    assert "<pico:checkpoint>" not in text
    # And DROP_PRIORITY order must be honored — checkpoint appears before
    # any later-priority source in the dropped list.
    dropped = tele["injection_dropped"]
    assert dropped.index("checkpoint") < dropped.index("workspace_state")


def test_intent_matched_reason_populated_for_keyword_hit():
    from unittest.mock import MagicMock

    from pico.context.renderer import render_current_user_message

    a = MagicMock()
    a.workspace = MagicMock(volatile_text=lambda: "")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.session = {"recently_recalled": [], "messages": []}
    a.memory_retrieval = None
    a.context_config = {}

    _text, tele = render_current_user_message(a, "上次报错了")
    # "报错" is a debug keyword and debug beats recall in _INTENT_ORDER.
    assert tele["intent"]["matched_reason"] == "keyword:'报错' via profile:debug"


def test_intent_matched_reason_default_when_no_keyword():
    from unittest.mock import MagicMock

    from pico.context.renderer import render_current_user_message

    a = MagicMock()
    a.workspace = MagicMock(volatile_text=lambda: "")
    a.memory_store = None
    a.repo_map = None
    a.render_checkpoint_text = MagicMock(return_value="")
    a.model_client = MagicMock(count_tokens=lambda t: max(1, len(t) // 4))
    a.session = {"recently_recalled": [], "messages": []}
    a.memory_retrieval = None
    a.context_config = {}

    _text, tele = render_current_user_message(a, "hello world")
    assert tele["intent"]["matched_reason"] == "default (no keyword)"
