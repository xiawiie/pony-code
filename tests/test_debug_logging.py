"""Task C3: silent catches now emit debug logs on the 'pico' logger.

Behavior is unchanged — the catches still return ``None`` (or the
appropriate fallback). But when a user opts in via
``logging.basicConfig(level=logging.DEBUG)`` (or configures a ``pico``
logger explicitly), each previously-silent failure surfaces a debug
line. This makes silent-drop debugging tractable without changing any
control flow.
"""

import logging



def test_recall_failure_logs_debug(caplog, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from pico.context.renderer import render_current_user_message

    def _boom(*a, **kw):
        raise RuntimeError("simulated recall failure")

    monkeypatch.setattr("pico.memory.recall.recall_for_turn", _boom)

    a = SimpleNamespace(
        memory_store=MagicMock(),
        memory_retrieval=MagicMock(),
        session={"recently_recalled": [], "messages": []},
        workspace=MagicMock(volatile_text=lambda: ""),
        repo_map=None,
        render_checkpoint_text=lambda: "",
        model_client=MagicMock(count_tokens=lambda t: max(1, len(t) // 4)),
        memory=SimpleNamespace(task_summary=""),
        context_config={},
    )

    caplog.set_level(logging.DEBUG, logger="pico")
    render_current_user_message(a, "上次讨论 cache")
    assert any("recall" in r.message.lower() for r in caplog.records)


def test_workspace_state_failure_logs_debug(caplog, tmp_path):
    from unittest.mock import MagicMock

    from pico.context.sources import render_workspace_state

    a = MagicMock()
    a.workspace = MagicMock()
    a.workspace.volatile_text = MagicMock(side_effect=RuntimeError("no git"))

    caplog.set_level(logging.DEBUG, logger="pico")
    result = render_workspace_state(a, budget_tokens=500)
    assert result is None
    assert any("workspace_state" in r.message.lower() for r in caplog.records)
