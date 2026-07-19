from pathlib import Path
from types import SimpleNamespace

from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.cli.start import _process_repl_input, run_repl
from pony.runtime.options import RuntimeOptions
from pony.runtime.resume import active_prompt_history
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext


def _agent(tmp_path, outputs=(), *, bypass_permissions_available=False):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pony(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(
            project_trusted=True,
            bypass_permissions_available=bypass_permissions_available,
        ),
    )


def test_repl_permissions_manages_rules_and_same_value_is_noop(tmp_path, capsys):
    agent = _agent(tmp_path)

    def manager(_rules, _tools):
        return "allow", "write_file"

    _process_repl_input(agent, "/permissions", manage_permissions=manager)
    entries = len(agent.session_store.load_tree(agent.session["id"]).entries)
    _process_repl_input(agent, "/allowed-tools", manage_permissions=manager)
    _process_repl_input(agent, "/permissions manual")

    output = capsys.readouterr().out
    assert "mode: auto" in output
    assert "permission rule: allow write_file" in output
    assert "(unchanged)" in output
    assert "usage: /permissions" in output
    assert agent.permission_rules()["allow"] == ["write_file"]
    assert len(agent.session_store.load_tree(agent.session["id"]).entries) == entries


def test_repl_permissions_applies_multiple_rules_and_changes_mode(tmp_path, capsys):
    agent = _agent(tmp_path, bypass_permissions_available=True)

    def manager(_rules, _tools):
        return [
            ("allow", "write_file"),
            ("deny", "run_shell"),
            ("mode", "manual"),
        ]

    _process_repl_input(agent, "/permissions", manage_permissions=manager)

    assert agent.permission_rules() == {
        "allow": ["write_file"],
        "ask": [],
        "deny": ["run_shell"],
    }
    assert agent.current_permission_mode() == "default"
    assert "permission mode: manual" in capsys.readouterr().out


def test_repl_permissions_lists_legal_tools_hidden_by_runtime_allowlist(tmp_path):
    agent = _agent(tmp_path)
    agent.tools = {"read_file": agent.tools["read_file"]}
    seen = []

    _process_repl_input(
        agent,
        "/permissions",
        manage_permissions=lambda _rules, tools: seen.extend(tools),
    )

    assert "read_file" in seen
    assert "write_file" in seen


def test_repl_plan_enters_plan_permission_mode(tmp_path, capsys):
    agent = _agent(tmp_path)
    before = len(agent.session_store.load_tree(agent.session["id"]).entries)

    _process_repl_input(agent, "/plan")

    output = capsys.readouterr().out
    assert "permission mode: plan" in output
    tree = agent.session_store.load_tree(agent.session["id"])
    assert agent.session["permission_mode"] == "plan"
    assert len(tree.entries) == before + 1
    assert tree.entries[-1]["type"] == "permission_mode_change"


def test_repl_plan_open_enters_plan_and_skips_editor_without_artifact(
    tmp_path, monkeypatch, capsys
):
    agent = _agent(tmp_path)
    monkeypatch.setenv("EDITOR", "pony-test-editor")
    monkeypatch.setattr(
        "pony.cli.start.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("editor must not open without a saved plan")
        ),
    )

    _process_repl_input(agent, "/plan open")

    assert agent.current_permission_mode() == "plan"
    assert agent.current_plan() == ""
    output = capsys.readouterr().out
    assert "permission mode: plan" in output
    assert "(no plan saved)" in output


def test_repl_plan_open_edits_existing_artifact_in_plan_mode(
    tmp_path, monkeypatch, capsys
):
    agent = _agent(tmp_path)
    agent.set_permission_mode("plan")
    agent.save_plan_text("# Original Plan")
    agent.set_permission_mode("auto")
    before = len(agent.session_store.load_tree(agent.session["id"]).entries)
    monkeypatch.setenv("EDITOR", "pony-test-editor")
    monkeypatch.setattr("pony.cli.start.shutil.which", lambda _name: "/usr/bin/editor")

    def edit(argv, **_kwargs):
        Path(argv[-1]).write_text("# Edited Plan\n1. Test\n", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("pony.cli.start.subprocess.run", edit)

    _process_repl_input(agent, "/plan open")

    tree = agent.session_store.load_tree(agent.session["id"])
    assert agent.current_permission_mode() == "plan"
    assert agent.current_plan() == "# Edited Plan\n1. Test"
    assert len(tree.entries) == before + 2
    assert tree.entries[-1]["type"] == "plan_artifact"
    output = capsys.readouterr().out
    assert "permission mode: plan" in output
    assert "Opened plan in editor" in output


def test_repl_plan_share_enters_plan_before_reporting_unavailable(tmp_path, capsys):
    agent = _agent(tmp_path)
    before = len(agent.session_store.load_tree(agent.session["id"]).entries)

    _process_repl_input(agent, "/plan share")

    assert agent.current_permission_mode() == "plan"
    assert len(agent.session_store.load_tree(agent.session["id"]).entries) == before + 1
    output = capsys.readouterr().out
    assert "permission mode: plan" in output
    assert "plan sharing is unavailable" in output


def test_removed_mode_is_unknown_and_plan_description_is_submitted(tmp_path, capsys):
    agent = _agent(tmp_path, outputs=("planned",))

    _process_repl_input(agent, "/mode")
    _process_repl_input(agent, "/plan clear")

    output = capsys.readouterr().out
    assert "unknown command: /mode" in output
    assert "planned" in output
    assert agent.session["permission_mode"] == "plan"
    assert any(
        message.get("role") == "user" and message.get("content") == "clear"
        for message in agent.session["messages"]
    )


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
        "permission_mode": "default",
        "messages": [],
        "checkpoints": {
            "current_id": "checkpoint",
            "items": {
                "checkpoint": {
                    "goal": "Ship permission controls",
                    "status": "ready",
                }
            },
        },
        "resume_state": {"status": "ready"},
        "provider_binding": {
            "protocol_family": "openai_responses",
            "model": "gpt-test",
            "endpoint_hash": "sha256:" + "a" * 64,
        },
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
    assert "permission [session]: manual" in output
    assert "goal [checkpoint]: Ship permission controls" in output
    assert "resume [resume_state]: ready" in output
    assert "model [provider_binding]: openai_responses/gpt-test" in output
    assert "endpoint_hash" not in output


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
