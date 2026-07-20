from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pony.agent.compaction import CompactionNoProgress
from pony.cli.arguments import build_arg_parser
from pony.cli.commands import handle_session
from pony.cli.errors import CliError
from pony.cli.session import handle_session_command
from pony.cli.start import (
    _MAX_SESSION_PICKER_CANDIDATES,
    _handle_repl_session_command,
    _plain_session_picker,
    _session_branch_candidates,
)
from pony.state.session_store import SessionTree


def test_public_session_checkpoint_builds_runtime_from_assembly(
    tmp_path,
    monkeypatch,
    capsys,
):
    agent = MagicMock()
    agent.create_manual_checkpoint.return_value = {
        "checkpoint_id": "checkpoint-1",
        "label": "milestone",
    }
    built = []

    def build_agent(args):
        built.append(args.resume)
        return agent

    monkeypatch.setattr("pony.cli.assembly.build_agent", build_agent)
    args = build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    code = handle_session(
        ["checkpoint", "session-1", "milestone"],
        tmp_path,
        args,
    )

    assert code == 0
    assert built == ["session-1"]
    assert "checkpoint: checkpoint-1" in capsys.readouterr().out
    agent.create_manual_checkpoint.assert_called_once_with("milestone")


def test_public_session_compaction_preserves_typed_no_progress_error(
    tmp_path,
    monkeypatch,
):
    agent = MagicMock()
    agent.compact_session.side_effect = CompactionNoProgress(
        "compaction_no_progress: active tail already fits"
    )
    monkeypatch.setattr("pony.cli.assembly.build_agent", lambda _args: agent)
    args = build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with pytest.raises(CliError) as raised:
        handle_session(
            ["compact", "session-1"],
            tmp_path,
            args,
        )

    assert raised.value.code == "compaction_no_progress"
    assert raised.value.exit_code == 1


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


def test_repl_bare_fork_uses_picker_and_reloads_history():
    agent = MagicMock()
    agent.redact_text.side_effect = str
    agent.session = {"id": "session-1"}
    agent.session_store.load_tree.return_value = SimpleNamespace(
        leaf_id="leaf",
        entries=(
            {"id": "entry-1", "type": "message", "parent_id": "", "data": {}},
            {"id": "leaf", "type": "message", "parent_id": "entry-1", "data": {}},
        ),
        active_path=(),
    )
    agent.fork_session.return_value = {"id": "fork-1", "parent_id": "entry-1"}
    refreshed = []

    handled = _handle_repl_session_command(
        agent,
        "/fork",
        pick_session_entry=lambda command, candidates: (
            candidates == [("entry-1", "entry-1 | message | branch")]
            and command == "/fork"
            and "entry-1"
        ),
        refresh_history=lambda: refreshed.append(True),
    )

    assert handled is True
    agent.fork_session.assert_called_once_with(
        "entry-1",
        expected_leaf_id="leaf",
    )
    assert refreshed == [True]


def test_repl_bare_rewind_cancel_does_not_write():
    agent = MagicMock()
    agent.redact_text.side_effect = str
    agent.session = {"id": "session-1"}
    agent.session_store.load_tree.return_value = SimpleNamespace(
        leaf_id="leaf",
        entries=(
            {"id": "entry-1", "type": "message", "parent_id": "", "data": {}},
            {"id": "leaf", "type": "message", "parent_id": "entry-1", "data": {}},
        ),
        active_path=(),
    )

    handled = _handle_repl_session_command(
        agent,
        "/rewind",
        pick_session_entry=lambda _command, _candidates: None,
    )

    assert handled is True
    agent.rewind_session.assert_not_called()


def test_repl_bare_rewind_accepts_summary_option_after_picker():
    agent = MagicMock()
    agent.redact_text.side_effect = str
    agent.session = {"id": "session-1"}
    agent.session_store.load_tree.return_value = SimpleNamespace(
        leaf_id="leaf",
        entries=(
            {"id": "entry-1", "type": "message", "parent_id": "", "data": {}},
            {"id": "leaf", "type": "message", "parent_id": "entry-1", "data": {}},
        ),
        active_path=(),
    )
    agent.rewind_session.return_value = SimpleNamespace(
        rewind_entry={"id": "rewind-1", "parent_id": "entry-1"}
    )

    handled = _handle_repl_session_command(
        agent,
        "/rewind --summary=carry-tests",
        pick_session_entry=lambda _command, _candidates: "entry-1",
    )

    assert handled is True
    agent.rewind_session.assert_called_once_with(
        "entry-1",
        summary=True,
        focus="carry-tests",
        expected_leaf_id="leaf",
    )


def test_repl_bare_picker_rejects_entry_not_in_current_tree(capsys):
    agent = MagicMock()
    agent.redact_text.side_effect = str
    agent.session = {"id": "session-1"}
    agent.session_store.load_tree.return_value = SimpleNamespace(
        leaf_id="leaf",
        entries=(
            {"id": "entry-1", "type": "message", "parent_id": "", "data": {}},
            {"id": "leaf", "type": "message", "parent_id": "entry-1", "data": {}},
        ),
        active_path=(),
    )

    handled = _handle_repl_session_command(
        agent,
        "/rewind",
        pick_session_entry=lambda _command, _candidates: "not-an-entry",
    )

    assert handled is True
    assert "selected session entry is unavailable" in capsys.readouterr().out
    agent.rewind_session.assert_not_called()


def test_plain_session_picker_selects_number_and_cancels(monkeypatch, capsys):
    candidates = [("entry-1", "entry-1 | message | active")]
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")

    assert _plain_session_picker("/fork", candidates) == "entry-1"
    assert "1. entry-1 | message | active" in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert _plain_session_picker("/rewind", candidates) is None


@pytest.mark.parametrize("selected", ("0", "-1", "2"))
def test_plain_session_picker_rejects_numbers_outside_the_list(
    monkeypatch,
    selected,
):
    candidates = [("entry-1", "entry-1 | message | active")]
    monkeypatch.setattr("builtins.input", lambda _prompt: selected)

    with pytest.raises(ValueError, match="choose a listed session entry"):
        _plain_session_picker("/fork", candidates)


def test_session_picker_candidates_redact_and_strip_control_text():
    tree = SessionTree(
        header={},
        entries=(
            {
                "id": "entry-1",
                "type": "message",
                "parent_id": "",
                "data": {
                    "message": {"role": "user", "content": "api_key=secret\x1b[2J"}
                },
            },
            {"id": "leaf", "type": "message", "parent_id": "entry-1", "data": {}},
        ),
        active_path=(
            {"id": "entry-1"},
            {"id": "leaf"},
        ),
        projection={},
        entry_token_estimates={},
    )
    agent = SimpleNamespace(
        session={"id": "session-1"},
        session_store=SimpleNamespace(load_tree=lambda _session_id: tree),
        redact_text=lambda _text: "<redacted>",
    )

    assert _session_branch_candidates(agent) == [
        ("entry-1", "entry-1 | user: <redacted> | active"),
    ]


def test_session_picker_sanitizes_redactor_output():
    tree = SimpleNamespace(
        leaf_id="leaf",
        entries=(
            {
                "id": "entry-1",
                "type": "message",
                "parent_id": "",
                "data": {"message": {"role": "user", "content": "hello"}},
            },
            {"id": "leaf", "type": "message", "parent_id": "entry-1", "data": {}},
        ),
        active_path=(),
    )
    agent = SimpleNamespace(
        session={"id": "session-1"},
        session_store=SimpleNamespace(load_tree=lambda _session_id: tree),
        redact_text=lambda _text: "<redacted>\x1b[2J",
    )

    assert _session_branch_candidates(agent) == [
        ("entry-1", "entry-1 | user: <redacted> [2J | branch"),
    ]


def test_session_picker_candidates_are_bounded_to_recent_entries():
    entries = tuple(
        {
            "id": f"entry-{index}",
            "type": "message",
            "parent_id": "",
            "data": {},
        }
        for index in range(_MAX_SESSION_PICKER_CANDIDATES + 2)
    )
    tree = SimpleNamespace(
        leaf_id=entries[-1]["id"],
        entries=entries,
        active_path=(),
    )
    agent = SimpleNamespace(
        session={"id": "session-1"},
        session_store=SimpleNamespace(load_tree=lambda _session_id: tree),
    )

    candidates = _session_branch_candidates(agent)

    assert len(candidates) == _MAX_SESSION_PICKER_CANDIDATES
    assert candidates[0][0] == entries[-2]["id"]
    assert candidates[-1][0] == entries[-_MAX_SESSION_PICKER_CANDIDATES - 1]["id"]
