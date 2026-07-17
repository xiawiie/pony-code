from types import SimpleNamespace
from unittest.mock import MagicMock

from pony.cli.session import handle_session_command
from pony.cli.start import _handle_repl_session_command


def _preview():
    return {
        "status": "ready",
        "workspace_checkpoint_id": "recovery-1",
        "decision_counts": {"restore": 1, "skip": 1},
        "entries": [
            {"decision": "restore", "path": "a.py", "reason": "hash_match"},
            {"decision": "skip", "path": "b.py", "reason": "unchanged"},
        ],
    }


def test_noninteractive_workspace_rewind_previews_without_mutation(
    tmp_path,
    capsys,
):
    agent = MagicMock()
    agent.preview_workspace_rewind.return_value = _preview()

    code = handle_session_command(
        ["rewind", "session-1", "entry-1", "--workspace"],
        sessions_root=tmp_path,
        agent_factory=lambda _session_id: agent,
    )

    assert code == 1
    assert "confirmation_required" in capsys.readouterr().out
    agent.rewind_session.assert_not_called()


def test_noninteractive_workspace_rewind_yes_applies_once(tmp_path, capsys):
    agent = MagicMock()
    agent.preview_workspace_rewind.return_value = _preview()
    agent.rewind_session.return_value = SimpleNamespace(
        rewind_entry={"id": "rewind-1", "parent_id": "entry-1"},
        restore_result={"status": "applied", "restored_paths": ["a.py"]},
        summary_entry=None,
    )

    code = handle_session_command(
        ["rewind", "session-1", "entry-1", "--workspace", "--yes"],
        sessions_root=tmp_path,
        agent_factory=lambda _session_id: agent,
    )

    assert code == 0
    assert "restore_status: applied" in capsys.readouterr().out
    agent.rewind_session.assert_called_once_with(
        "entry-1",
        workspace=True,
        confirmed=True,
        summary=False,
        focus="",
    )


def test_noninteractive_manual_checkpoint_uses_runtime(tmp_path, capsys):
    agent = MagicMock()
    agent.create_manual_checkpoint.return_value = {
        "checkpoint_id": "checkpoint-1",
        "label": "milestone",
    }

    code = handle_session_command(
        ["checkpoint", "session-1", "milestone"],
        sessions_root=tmp_path,
        agent_factory=lambda _session_id: agent,
    )

    assert code == 0
    assert "checkpoint: checkpoint-1" in capsys.readouterr().out
    agent.create_manual_checkpoint.assert_called_once_with("milestone")


def test_repl_workspace_rewind_prompts_once_and_can_cancel(monkeypatch, capsys):
    agent = MagicMock()
    agent.preview_workspace_rewind.return_value = _preview()
    agent.redact_text.side_effect = str
    answers = iter(["no"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(answers))

    handled = _handle_repl_session_command(
        agent,
        "/rewind entry-1 --workspace",
    )

    assert handled is True
    assert "workspace rewind cancelled" in capsys.readouterr().out
    agent.rewind_session.assert_not_called()


def test_repl_workspace_rewind_confirmed_passes_summary_focus(
    monkeypatch,
    capsys,
):
    agent = MagicMock()
    agent.preview_workspace_rewind.return_value = _preview()
    agent.redact_text.side_effect = str
    agent.rewind_session.return_value = SimpleNamespace(
        rewind_entry={"id": "rewind-1", "parent_id": "entry-1"}
    )
    answers = iter(["yes"])
    monkeypatch.setattr("builtins.input", lambda *_args: next(answers))

    handled = _handle_repl_session_command(
        agent,
        "/rewind entry-1 --workspace --summary=carry-tests",
    )

    assert handled is True
    assert "rewound to entry-1" in capsys.readouterr().out
    agent.rewind_session.assert_called_once_with(
        "entry-1",
        summary=True,
        focus="carry-tests",
        workspace=True,
        confirmed=True,
    )
