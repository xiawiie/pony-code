from types import SimpleNamespace
from unittest.mock import MagicMock

from pony.cli.session import handle_session_command
from pony.cli.start import _handle_repl_session_command


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


def test_noninteractive_rewind_rejects_removed_workspace_option(tmp_path, capsys):
    agent = MagicMock()

    code = handle_session_command(
        ["rewind", "session-1", "entry-1", "--workspace"],
        sessions_root=tmp_path,
        agent_factory=lambda _session_id: agent,
    )

    assert code == 1
    assert "unknown rewind option: --workspace" in capsys.readouterr().out
    agent.rewind_session.assert_not_called()


def test_repl_rewind_rejects_removed_workspace_option(capsys):
    agent = MagicMock()
    agent.redact_text.side_effect = str

    handled = _handle_repl_session_command(
        agent,
        "/rewind entry-1 --workspace",
    )

    assert handled is True
    assert "unknown rewind option: --workspace" in capsys.readouterr().out
    agent.rewind_session.assert_not_called()


def test_repl_summary_rewind_keeps_session_only_behavior(capsys):
    agent = MagicMock()
    agent.redact_text.side_effect = str
    agent.rewind_session.return_value = SimpleNamespace(
        rewind_entry={"id": "rewind-1", "parent_id": "entry-1"}
    )

    handled = _handle_repl_session_command(
        agent,
        "/rewind entry-1 --summary=carry-tests",
    )

    assert handled is True
    assert "rewound to entry-1" in capsys.readouterr().out
    agent.rewind_session.assert_called_once_with(
        "entry-1",
        summary=True,
        focus="carry-tests",
    )
