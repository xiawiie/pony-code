from types import SimpleNamespace

from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.cli.start import _process_repl_input, run_repl
from pony.runtime.options import RuntimeOptions
from pony.runtime.resume import active_prompt_history
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext


def _agent(tmp_path, outputs=()):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pony(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(approval_policy="auto"),
    )


def _plan():
    return {
        "goal": "Ship workflow controls",
        "items": [
            {"id": "cli", "text": "Wire the CLI", "status": "in_progress"}
        ],
    }


def test_repl_mode_show_change_and_same_value_noop(tmp_path, capsys):
    agent = _agent(tmp_path)

    _process_repl_input(agent, "/mode")
    _process_repl_input(agent, "/mode plan")
    entries = len(agent.session_store.load_tree(agent.session["id"]).entries)
    _process_repl_input(agent, "/mode plan")

    output = capsys.readouterr().out
    assert "mode: act" in output
    assert "mode: plan" in output
    assert "mode: plan (unchanged)" in output
    assert len(agent.session_store.load_tree(agent.session["id"]).entries) == entries


def test_repl_plan_shows_full_state_and_clear_is_append_only(tmp_path, capsys):
    agent = _agent(tmp_path)
    agent.session_store.set_active_plan(agent.session["id"], _plan())
    agent._reload_session_projection()
    before = len(agent.session_store.load_tree(agent.session["id"]).entries)

    _process_repl_input(agent, "/plan")
    _process_repl_input(agent, "/plan clear")

    output = capsys.readouterr().out
    assert "Ship workflow controls" in output
    assert '"status": "in_progress"' in output
    tree = agent.session_store.load_tree(agent.session["id"])
    assert agent.session["active_plan"] == {"goal": "", "items": []}
    assert len(tree.entries) == before + 1
    assert tree.entries[-1]["type"] == "plan_update"


def test_plan_clear_fails_with_stable_active_turn_error(tmp_path, capsys):
    agent = _agent(tmp_path)
    agent._workflow_turn = {"mode": "act", "plan": _plan(), "tools": {}}

    _process_repl_input(agent, "/plan clear")

    assert "error: workflow_turn_active" in capsys.readouterr().out
    assert agent.session["active_plan"] == {"goal": "", "items": []}


def test_reset_rebuilds_history_from_active_messages_immediately():
    agent = SimpleNamespace(
        session={"messages": [{"role": "user", "content": "old"}]},
    )

    def reset():
        agent.session = {"messages": [{"role": "user", "content": "new branch"}]}

    agent.reset = reset
    refreshed = []
    _process_repl_input(
        agent,
        "/reset",
        refresh_history=lambda: refreshed.extend(
            active_prompt_history(agent.session["messages"])
        ),
    )

    assert refreshed == ["new branch"]


def test_plain_explicit_resume_card_is_shown_once_with_sources(monkeypatch, capsys):
    session = {
        "workflow_mode": "review",
        "active_plan": _plan(),
        "messages": [],
        "checkpoints": {"current_id": "", "items": {}},
        "resume_state": {"status": "ready"},
    }
    agent = SimpleNamespace(
        session=session,
        redact_artifact=lambda value: value,
        finalize_sandbox_session=lambda: None,
    )
    inputs = iter(("/exit",))
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

    assert run_repl(agent, plain=True, show_resume=True) == 0
    output = capsys.readouterr().out
    assert output.count("Resume\n") == 1
    assert "mode [session]: review" in output
    assert "goal [plan]: Ship workflow controls" in output
    assert "resume [resume_state]: ready" in output


def test_plain_history_is_rebuilt_from_canonical_prompts_after_every_input(
    tmp_path,
    monkeypatch,
):
    import readline

    agent = _agent(tmp_path, outputs=("done",))
    inputs = iter(("/help", "inspect canonical state", "/exit"))
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

    assert run_repl(agent, plain=True) == 0
    history = [
        readline.get_history_item(index)
        for index in range(1, readline.get_current_history_length() + 1)
    ]
    assert history == ["inspect canonical state"]
