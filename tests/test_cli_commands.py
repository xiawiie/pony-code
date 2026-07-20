import getpass
import io
import json
import os
import stat

import pytest

from pony.cli.app import main
from pony.cli.assembly import _model_client_factory
from pony.config.environment import read_project_env
from pony.config.model import resolve_model_config
from pony.providers.transport import ProviderTransportError
from pony.runtime.options import RuntimeOptions


def _install_fake_agent(monkeypatch, tmp_path, called, *, permission_mode="default"):
    def fake_build_agent(args):
        called["built"] = True
        called["prompt"] = list(getattr(args, "prompt", []))

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            session = {"id": "s", "permission_mode": permission_mode}
            session_path = str(tmp_path / ".pony" / "sessions" / "s.json")
            tools = {"read_file": {}, "write_file": {}, "run_shell": {}}

            def ask(self, message):
                called["asked"] = message
                called["bypass_available"] = getattr(
                    self, "bypass_permissions_available", False
                )
                return "answer"

            def set_permission_mode(self, mode):
                called["permission_mode"] = mode
                self.session["permission_mode"] = mode

            def current_permission_mode(self):
                return self.session["permission_mode"]

            def set_permission_rule(self, name, behavior):
                called.setdefault("permission_rules", []).append((behavior, name))

            def set_permission_rules(self, updates):
                called.setdefault("permission_rules", []).extend(
                    (behavior, name) for name, behavior in updates
                )

            def update_permissions(self, *, mode=None, rule_updates=()):
                if mode is not None:
                    self.set_permission_mode(mode)
                self.set_permission_rules(rule_updates)
                return {"mode_entry": mode, "rules": tuple(rule_updates) or None}

            def memory_text(self):
                return "memory"

            def reset(self):
                called["reset"] = True

        agent = FakeAgent()
        agent.bypass_permissions_available = bool(
            getattr(args, "allow_dangerously_skip_permissions", False)
            or getattr(args, "dangerously_skip_permissions", False)
        )
        if getattr(args, "resume", None) and getattr(args, "permission_mode", None):
            agent.set_permission_mode(args.permission_mode)
        return agent

    monkeypatch.setattr("pony.cli.app.build_agent", fake_build_agent)


def test_model_client_factory_rebuilds_the_resolved_transport():
    config = {
        "protocol": {"value": "openai_chat_completions"},
        "model": {"value": "gpt-test"},
        "base_url": {"value": "https://api.example/v1"},
        "api_key": {"value": "test-key"},
        "auth_mode": {"value": "bearer"},
        "capabilities": {"strict_tools": True},
    }

    factory = _model_client_factory(config, 30)
    first = factory()
    second = factory()
    replacement = factory("gpt-next")

    assert second is not first
    assert second.provider_binding == first.provider_binding
    assert second.capabilities == first.capabilities
    assert replacement.model == "gpt-next"


def test_run_command_calls_agent_once(tmp_path, monkeypatch, capsys):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    code = main(["--cwd", str(tmp_path), "run", "fix", "tests"])

    assert code == 0
    assert called["asked"] == "fix tests"
    assert "answer" in capsys.readouterr().out


def test_run_permission_mode_is_applied_after_runtime_build(tmp_path, monkeypatch):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    assert (
        main(
            [
                "--cwd",
                str(tmp_path),
                "--permission-mode",
                "plan",
                "run",
                "inspect",
            ]
        )
        == 0
    )
    assert called["permission_mode"] == "plan"


def test_permission_mode_is_rejected_for_management_commands(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        "pony.cli.app.build_agent",
        lambda _args: pytest.fail("agent must not be built"),
    )

    assert (
        main(
            [
                "--cwd",
                str(tmp_path),
                "--permission-mode",
                "dontAsk",
                "status",
            ]
        )
        == 2
    )
    assert "permission flags are only valid" in capsys.readouterr().err


def test_model_flag_is_rejected_for_management_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "pony.cli.app.build_agent",
        lambda _args: pytest.fail("agent must not be built"),
    )

    assert main(["--cwd", str(tmp_path), "--model", "gpt-next", "status"]) == 2
    assert "--model is only valid" in capsys.readouterr().err


def test_bypass_permission_mode_requires_explicit_dangerous_opt_in(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        "pony.cli.app.build_agent",
        lambda _args: pytest.fail("agent must not be built"),
    )

    assert (
        main(
            [
                "--cwd",
                str(tmp_path),
                "--permission-mode",
                "bypassPermissions",
                "run",
                "inspect",
            ]
        )
        == 2
    )
    assert "requires --allow-dangerously-skip-permissions" in capsys.readouterr().err


def test_direct_bypass_flag_applies_session_mode(tmp_path, monkeypatch):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    assert (
        main(
            [
                "--cwd",
                str(tmp_path),
                "--dangerously-skip-permissions",
                "run",
                "inspect",
            ]
        )
        == 0
    )
    assert called["permission_mode"] == "bypassPermissions"


def test_permission_rule_flags_share_one_parser_and_deny_wins(
    tmp_path, monkeypatch
):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--allowed-tools",
            "read_file,write_file",
            "--disallowedTools",
            "write_file run_shell",
            "run",
            "inspect",
        ]
    )

    assert code == 0
    assert called["permission_rules"] == [
        ("allow", "read_file"),
        ("allow", "write_file"),
        ("deny", "write_file"),
        ("deny", "run_shell"),
    ]


@pytest.mark.parametrize(
    "permission_args",
    (
        [
            "--allowed-tools",
            "read_file",
            "--allowed-tools",
            "unknown_permission_tool",
        ],
        [
            "--permission-mode",
            "plan",
            "--allowed-tools",
            "unknown_permission_tool",
        ],
    ),
)
def test_invalid_permission_flags_leave_real_resumed_session_unchanged(
    tmp_path,
    monkeypatch,
    capsys,
    permission_args,
):
    from benchmarks.support.fake_provider import FakeModelClient
    from pony import Pony
    from pony.state.session_store import SessionStore
    from pony.workspace.context import WorkspaceContext

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    store = SessionStore(tmp_path / ".pony" / "sessions")
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=store,
        options=RuntimeOptions(
            project_trusted=True,
            allow_dangerously_skip_permissions=True,
        ),
    )
    session_id = agent.session["id"]
    session_path = store.path(session_id)
    original = session_path.read_bytes()
    monkeypatch.setattr(
        "pony.cli.app.build_agent",
        lambda _args: pytest.fail("invalid permission flags must fail before build"),
    )

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--resume",
            session_id,
            *permission_args,
            "run",
            "inspect",
        ]
    )

    assert code == 2
    assert "unknown permission rule tool" in capsys.readouterr().err
    assert session_path.read_bytes() == original
    assert store.load(session_id)["permission_mode"] == "auto"
    assert store.load(session_id)["permission_rules"] == {
        "allow": [],
        "ask": [],
        "deny": [],
    }


def test_plain_resume_of_bypass_requires_dangerous_capability(
    tmp_path, monkeypatch, capsys
):
    called = {}
    _install_fake_agent(
        monkeypatch,
        tmp_path,
        called,
        permission_mode="bypassPermissions",
    )

    denied = main(["--cwd", str(tmp_path), "--resume", "s", "run", "inspect"])
    allowed = main(
        [
            "--cwd",
            str(tmp_path),
            "--resume",
            "s",
            "--allow-dangerously-skip-permissions",
            "run",
            "inspect",
        ]
    )

    assert denied == 2
    assert allowed == 0
    assert "resuming bypassPermissions requires" in capsys.readouterr().err
    assert called["bypass_available"] is True


def test_resume_of_bypass_can_explicitly_restore_manual_without_opt_in(
    tmp_path, monkeypatch
):
    called = {}
    _install_fake_agent(
        monkeypatch,
        tmp_path,
        called,
        permission_mode="bypassPermissions",
    )

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--resume",
            "s",
            "--permission-mode",
            "manual",
            "run",
            "inspect",
        ]
    )

    assert code == 0
    assert called["permission_mode"] == "manual"


def test_real_resume_bypass_preflight_runs_before_provider_resolution(
    tmp_path, monkeypatch
):
    from benchmarks.support.fake_provider import FakeModelClient
    from pony import Pony
    from pony.cli.arguments import build_arg_parser
    from pony.cli.assembly import _build_agent
    from pony.cli.errors import CliError
    from pony.state.session_store import SessionStore
    from pony.workspace.context import WorkspaceContext

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(
            project_trusted=True,
            allow_dangerously_skip_permissions=True,
        ),
    )
    agent.set_permission_mode("bypassPermissions")
    args = build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--resume", agent.session["id"], "run", "inspect"]
    )
    monkeypatch.setattr(
        "pony.cli.assembly._build_transport_client",
        lambda *_args, **_kwargs: pytest.fail("provider resolution must not run"),
    )

    with pytest.raises(CliError, match="resuming bypassPermissions"):
        _build_agent(args, workspace)


def test_invalid_no_input_repl_is_rejected_before_agent_build(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        "pony.cli.app.build_agent",
        lambda _args: pytest.fail("agent must not be built"),
    )

    assert (
        main(
            [
                "--cwd",
                str(tmp_path),
                "--no-input",
                "--permission-mode",
                "acceptEdits",
                "repl",
            ]
        )
        == 2
    )
    assert "--no-input cannot be used" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("extra", "expected"),
    (([], True), (["--format", "json"], False)),
)
def test_explicit_resume_card_is_enabled_only_for_text_repl(
    tmp_path,
    monkeypatch,
    extra,
    expected,
):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)
    monkeypatch.setattr(
        "pony.cli.app.run_repl",
        lambda _agent, **options: called.update(options) or 0,
    )

    assert main(["--cwd", str(tmp_path), "--resume", "session", *extra, "repl"]) == 0
    assert called["show_resume"] is expected


@pytest.mark.parametrize("command", ([], ["repl"]))
def test_bare_and_explicit_repl_exit_on_eof(tmp_path, monkeypatch, command):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)
    monkeypatch.setattr(
        "builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError())
    )

    code = main(["--cwd", str(tmp_path), *command])

    assert code == 0
    assert called["built"] is True


@pytest.mark.parametrize(
    ("tokens", "usage"),
    [(["run"], "usage: pony run <prompt...>"), (["repl", "extra"], "usage: pony repl")],
)
def test_invalid_agent_command_arity_does_not_build_agent(
    tmp_path, monkeypatch, capsys, tokens, usage
):
    monkeypatch.setattr(
        "pony.cli.app.build_agent",
        lambda args: (_ for _ in ()).throw(AssertionError("must not build agent")),
    )

    code = main(["--cwd", str(tmp_path), *tokens])

    assert code == 2
    assert usage in capsys.readouterr().err


def test_bare_prompt_is_rejected_without_building_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "pony.cli.app.build_agent",
        lambda args: (_ for _ in ()).throw(AssertionError("must not build agent")),
    )

    code = main(["--cwd", str(tmp_path), "fix", "tests"])

    assert code == 2
    assert "Unknown command: fix" in capsys.readouterr().err


def test_help_command_shows_examples(capsys):
    code = main(["help"])

    assert code == 0
    out = capsys.readouterr().out
    assert "pony — Local coding agent" in out
    assert "USAGE:" in out
    assert "Available Commands:" in out
    assert "pony [global options]" in out
    assert "pony\n" in out
    assert "also the default for bare `pony`" in out
    assert "--no-color" in out
    assert "--sandbox" not in out
    assert "sandbox      " not in out
    assert 'pony run "inspect the failing tests"' in out
    assert "pony config set-secret PONY_API_KEY" in out
    assert "pony --permission-mode manual run" in out
    assert "pony checkpoints show <checkpoint-id>" in out
    assert "pony checkpoints pending" in out
    assert "pony runs summary latest" in out
    assert "migrate      Inspect and apply explicit artifact migrations" in out
    assert "Compatibility:" not in out
    assert "permission, path, and secret checks" in out
    assert "providers list" not in out


def test_removed_sandbox_command_is_rejected(capsys):
    code = main(["sandbox", "status"])

    assert code == 2
    assert "Unknown command: sandbox" in capsys.readouterr().err


def test_help_flag_uses_root_help_without_argparse_dump(capsys):
    code = main(["--help"])

    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("pony — Local coding agent")
    assert "Available Commands:" in out
    assert "positional arguments:" not in out


def test_unknown_command_suggests_close_match(capsys):
    code = main(["chekpoints", "list"])

    assert code == 2
    err = capsys.readouterr().err
    assert "Unknown command: chekpoints" in err
    assert "Did you mean `checkpoints`?" in err


@pytest.mark.parametrize("tokens", [["hello"], ["start", "a", "project"]])
def test_natural_language_requires_explicit_run(
    tmp_path,
    monkeypatch,
    tokens,
    capsys,
):
    monkeypatch.setattr(
        "pony.cli.app.build_agent",
        lambda args: (_ for _ in ()).throw(AssertionError("must not build agent")),
    )

    code = main(["--cwd", str(tmp_path), *tokens])

    assert code == 2
    assert f"Unknown command: {tokens[0]}" in capsys.readouterr().err


def test_unknown_command_suggestion_uses_json_error_envelope(capsys):
    code = main(["--format", "json", "chekpoints", "list"])

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unknown_command"
    assert payload["error"]["message"] == "Unknown command: chekpoints"
    assert payload["error"]["hint"] == "Did you mean `checkpoints`?"


def _install_init_input(
    monkeypatch,
    *,
    provider="anthropic",
    api_base="",
    model="",
    key="test-key",
):
    answers = iter((provider, api_base, model))
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda prompt: key)


def _install_successful_detection(monkeypatch, provider):
    def detect(config, **_kwargs):
        resolved = resolve_model_config(
            project_env={
                "PONY_PROVIDER": provider,
                "PONY_API_BASE": config["base_url"]["value"],
                "PONY_API_KEY": config["api_key"]["value"],
                "PONY_MODEL": config["model"]["value"],
            },
            process_env={},
        )
        return object(), resolved, {
            "status": "ok",
            "stage": "complete",
            "category": "ok",
            "model_calls": 2,
            "candidate_count": 1,
            "usage_status": "degraded",
        }

    monkeypatch.setattr("pony.cli.commands.resolve_provider_client", detect)


def test_init_auto_detects_before_writing_without_building_agent(
    tmp_path, monkeypatch, capsys
):
    _install_init_input(
        monkeypatch,
        provider="",
        api_base="https://gateway.example/v1",
        model="gateway-model",
    )
    _install_successful_detection(monkeypatch, "openai-chat")
    monkeypatch.setattr(
        "pony.cli.app.build_agent",
        lambda args: (_ for _ in ()).throw(AssertionError("init built an agent")),
    )
    monkeypatch.setattr(
        "pony.providers.transport._provider_urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("init attempted a request")
        ),
    )

    assert main(["--cwd", str(tmp_path), "init"]) == 0

    values = read_project_env(tmp_path, warn=False)
    assert values == {
        "PONY_PROVIDER": "openai-chat",
        "PONY_API_BASE": "https://gateway.example/v1",
        "PONY_MODEL": "gateway-model",
        "PONY_API_KEY": "test-key",
    }
    captured = capsys.readouterr()
    assert "Provider [auto]:" in captured.err
    assert "Detecting provider..." in captured.err
    assert captured.out.startswith("Pony init")
    assert "gateway-model" in captured.out
    assert "openai_chat_completions" in captured.out
    assert "usage" in captured.out and "unavailable" in captured.out
    assert "test-key" not in captured.out + captured.err


def test_init_forced_provider_performs_no_detection(tmp_path, monkeypatch):
    _install_init_input(monkeypatch, provider="anthropic")
    monkeypatch.setattr(
        "pony.cli.commands.resolve_provider_client",
        lambda *_args, **_kwargs: pytest.fail("forced Provider was probed"),
    )

    assert main(["--cwd", str(tmp_path), "init"]) == 0
    assert read_project_env(tmp_path, warn=False)["PONY_PROVIDER"] == "anthropic"


def test_init_detection_failure_leaves_existing_env_identity_unchanged(
    tmp_path,
    monkeypatch,
):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PONY_PROVIDER=auto\n"
        "PONY_API_BASE=https://gateway.example/v1\n"
        "PONY_API_KEY=existing-key\n"
        "PONY_MODEL=gateway-model\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    before = env_path.stat()
    before_bytes = env_path.read_bytes()
    _install_init_input(monkeypatch, provider="", key="")

    def fail(*_args, **_kwargs):
        raise ProviderTransportError(
            "unsafe raw failure",
            code="provider_detection_failed",
            stage="tool_call",
            protocol_reason="tool_call_shape_invalid",
        )

    monkeypatch.setattr("pony.cli.commands.resolve_provider_client", fail)

    assert main(["--cwd", str(tmp_path), "init"]) == 1

    after = env_path.stat()
    assert env_path.read_bytes() == before_bytes
    assert (after.st_ino, after.st_mtime_ns, stat.S_IMODE(after.st_mode)) == (
        before.st_ino,
        before.st_mtime_ns,
        stat.S_IMODE(before.st_mode),
    )


def test_init_accepts_exact_third_party_api_base(tmp_path, monkeypatch, capsys):
    _install_init_input(
        monkeypatch,
        provider="openai",
        api_base="https://lumina.tripo3d.com/v1/",
        key="gateway-key",
    )
    _install_successful_detection(monkeypatch, "openai-chat")

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "init",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    payload = json.loads(output)["data"]
    assert payload["api_base"] == "https://lumina.tripo3d.com/v1"
    assert payload["provider"] == "openai-chat"
    assert payload["model"] == "gpt-5.4"
    assert payload["protocol"] == "openai_chat_completions"
    assert payload["api_key"] == {
        "present": True,
        "name": "PONY_API_KEY",
    }
    assert "gateway-key" not in output


def test_init_can_select_openai_from_api_base(tmp_path, monkeypatch):
    _install_init_input(
        monkeypatch,
        provider="openai",
        api_base="https://api.openai.com/v1",
        key="openai-key",
    )
    _install_successful_detection(monkeypatch, "openai-responses")

    assert main(["--cwd", str(tmp_path), "init"]) == 0

    assert read_project_env(tmp_path, warn=False) == {
        "PONY_PROVIDER": "openai-responses",
        "PONY_API_BASE": "https://api.openai.com/v1",
        "PONY_MODEL": "gpt-5.4",
        "PONY_API_KEY": "openai-key",
    }


def test_init_can_configure_local_ollama_without_api_key(tmp_path, monkeypatch):
    _install_init_input(
        monkeypatch,
        provider="ollama",
        api_base="http://127.0.0.1:11434",
        key="",
    )

    assert main(["--cwd", str(tmp_path), "init"]) == 0

    assert read_project_env(tmp_path, warn=False) == {
        "PONY_PROVIDER": "ollama",
        "PONY_API_BASE": "http://127.0.0.1:11434",
        "PONY_MODEL": "qwen3:8b",
        "PONY_API_KEY": "",
    }


def test_init_empty_key_keeps_existing_project_key(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=anthropic\n"
        "PONY_API_BASE=https://old.example/v1\n"
        "PONY_MODEL=claude-sonnet-4-6\n"
        "PONY_API_KEY=existing-key\n",
        encoding="utf-8",
    )
    _install_init_input(monkeypatch, api_base="", key="")

    assert main(["--cwd", str(tmp_path), "init"]) == 0

    values = read_project_env(tmp_path, warn=False)
    assert values["PONY_API_BASE"] == "https://old.example/v1"
    assert values["PONY_API_KEY"] == "existing-key"
    captured = capsys.readouterr()
    assert "press Enter to keep existing" not in captured.out
    assert "existing-key" not in captured.out + captured.err


def test_init_updates_config_without_dropping_unrelated_lines(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "# keep this comment\n"
        "OTHER_SETTING=kept\n"
        "PONY_PROVIDER=anthropic\n"
        "PONY_API_BASE=https://old.example/v1\n"
        "PONY_MODEL=claude-sonnet-4-6\n"
        "PONY_API_KEY=old-key\n",
        encoding="utf-8",
    )
    _install_init_input(
        monkeypatch,
        api_base="https://new.example/v1",
        key="new-key",
    )

    assert main(["--cwd", str(tmp_path), "init"]) == 0

    text = (tmp_path / ".env").read_text(encoding="utf-8")
    values = read_project_env(tmp_path, warn=False)
    assert "# keep this comment\n" in text
    assert "OTHER_SETTING=kept\n" in text
    assert values["PONY_API_BASE"] == "https://new.example/v1"
    assert values["PONY_API_KEY"] == "new-key"


@pytest.mark.parametrize(
    "api_base",
    [
        "https://user:password@example.com/v1",
        "https://example.com/v1?token=x",
        "https://example.com/v1#fragment",
        "http://example.com/v1",
        "not-a-url",
    ],
)
def test_init_rejects_invalid_base_before_key_prompt(
    tmp_path, monkeypatch, capsys, api_base
):
    answers = iter(("", api_base))
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    key_prompt = pytest.fail
    monkeypatch.setattr(getpass, "getpass", key_prompt)

    assert main(["--cwd", str(tmp_path), "init"]) == 3

    captured = capsys.readouterr()
    assert "password" not in captured.out + captured.err
    assert not (tmp_path / ".env").exists()


def test_init_rejects_unsafe_existing_url_without_echo_or_prompt(
    tmp_path,
    monkeypatch,
    capsys,
):
    secret = "existing-url-secret-canary"
    (tmp_path / ".env").write_text(
        f"PONY_API_BASE=https://user:{secret}@example.com/v1\n"
        "PONY_API_KEY=existing-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda: (_ for _ in ()).throw(AssertionError("input called")),
    )
    monkeypatch.setattr(
        getpass,
        "getpass",
        lambda prompt: (_ for _ in ()).throw(AssertionError("getpass called")),
    )

    assert main(["--cwd", str(tmp_path), "init"]) == 3

    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "api_base_credentials" in captured.err


def test_init_rejects_empty_new_key_without_writing(tmp_path, monkeypatch, capsys):
    _install_init_input(monkeypatch, key="")

    assert main(["--cwd", str(tmp_path), "init"]) == 3

    assert "api_key_not_configured" in capsys.readouterr().err
    assert not (tmp_path / ".env").exists()


def test_init_invalid_provider_explains_configuration_error(
    tmp_path,
    monkeypatch,
    capsys,
):
    _install_init_input(monkeypatch, provider="unknown-provider")

    assert main(["--cwd", str(tmp_path), "init"]) == 3

    captured = capsys.readouterr()
    assert "Invalid Provider value" in captured.err
    assert "Choose auto or a Provider matching the API Base." in captured.err
    assert not (tmp_path / ".env").exists()


def test_init_no_input_never_prompts_or_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "builtins.input",
        lambda: (_ for _ in ()).throw(AssertionError("input called")),
    )
    monkeypatch.setattr(
        getpass,
        "getpass",
        lambda prompt: (_ for _ in ()).throw(AssertionError("getpass called")),
    )

    assert main(["--cwd", str(tmp_path), "--no-input", "init"]) == 2

    assert "requires interactive input" in capsys.readouterr().err
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize(
    "tokens",
    [
        ["--provider", "openai"],
        ["--profile", "official"],
        ["--base-url", "https://example.com/v1"],
        ["--api-key", "sk-cli-secret-123456789"],
        ["--connection", "legacy"],
    ],
)
def test_init_rejects_removed_arguments_without_writing_or_leaking(
    tmp_path, tokens, capsys
):
    secret = "sk-cli-secret-123456789"

    assert main(["--cwd", str(tmp_path), "init", *tokens]) == 2

    captured = capsys.readouterr()
    assert captured.err.strip() == "usage: pony init"
    assert secret not in captured.out + captured.err
    assert not (tmp_path / ".env").exists()


def test_config_set_secret_reads_stdin_and_writes_private_env(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-stdin-secret-123456789\n"))

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "config",
        "set-secret",
        "PONY_API_KEY",
        "--stdin",
            ]
        )
        == 0
    )

    assert read_project_env(tmp_path, warn=False)["PONY_API_KEY"] == (
        "sk-stdin-secret-123456789"
    )
    captured = capsys.readouterr()
    assert "sk-stdin" not in captured.out + captured.err
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600


def test_config_set_secret_uses_getpass_without_rendering_value(
    tmp_path, monkeypatch, capsys
):
    secret = "sk-getpass-secret-123456789"
    monkeypatch.setattr(getpass, "getpass", lambda prompt: secret)

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "config",
        "set-secret",
        "PONY_API_KEY",
            ]
        )
        == 0
    )

    assert read_project_env(tmp_path, warn=False)["PONY_API_KEY"] == secret
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "PONY_OPENAI_API_KEY",
        "PONY_DEEPSEEK_TOKEN",
    ],
)
def test_config_set_secret_rejects_every_other_name(
    tmp_path, monkeypatch, capsys, name
):
    monkeypatch.setattr(
        getpass,
        "getpass",
        lambda prompt: (_ for _ in ()).throw(AssertionError("getpass called")),
    )

    assert main(["--cwd", str(tmp_path), "config", "set-secret", name]) == 2

    assert "expected PONY_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / ".env").exists()


def test_config_set_secret_storage_failure_is_stable(tmp_path, monkeypatch, capsys):
    marker = "sk-sensitive-lock-path-123456789"
    outside = tmp_path.parent / marker
    outside.mkdir()
    (tmp_path / ".pony").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-input-secret-123456789\n"))

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "config",
        "set-secret",
        "PONY_API_KEY",
        "--stdin",
            ]
        )
        == 3
    )

    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err
    assert "project environment update failed" in captured.out + captured.err


def test_config_write_output_uses_canonical_project_env_metadata(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "repo"
    root.mkdir()
    _install_init_input(monkeypatch)

    assert (
        main(
            [
        "--cwd",
        str(root),
        "--format",
        "json",
        "init",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload["workspace"] == {"repo_root": str(root.resolve())}
    assert payload["project_env"] == {
        "path": str(root.resolve() / ".env"),
        "scope": "repo_root_exact",
        "status": "loaded",
    }
    assert "env_path" not in payload


def test_config_writes_redact_secret_shaped_workspace_path(
    tmp_path, monkeypatch, capsys
):
    marker = "sk-workspace-path-123456789"
    root = tmp_path / marker
    root.mkdir()
    _install_init_input(monkeypatch)

    assert (
        main(
            [
        "--cwd",
        str(root),
        "--format",
        "json",
        "init",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    data = json.loads(output)["data"]
    assert set(data["workspace"]) == {"repo_root"}
    assert data["project_env"]["scope"] == "repo_root_exact"
    assert marker not in output
    assert "test-key" not in output


def test_config_writes_keep_review_required_for_preserved_invalid_line(
    tmp_path, monkeypatch, capsys
):
    marker = "sk-" + "preserved-invalid-line-123456789"
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"PONY_API_BASE=https://api.deepseek.com\nPONY_API_KEY=old-key\n{marker}\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    _install_init_input(monkeypatch, key="new-key")

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "init",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    data = json.loads(captured.out)["data"]
    assert data["project_env"]["status"] == "review_required"
    assert marker not in captured.out + captured.err
    assert marker in env_path.read_text(encoding="utf-8")


def test_repl_help_renders_help_details(tmp_path, monkeypatch, capsys):
    from pony.cli.help import HELP_DETAILS
    from pony.cli.start import run_repl
    from benchmarks.support.fake_provider import FakeModelClient
    from pony.runtime.application import Pony
    from pony.state.session_store import SessionStore
    from pony.workspace.context import WorkspaceContext

    workspace = WorkspaceContext.build(tmp_path)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    session_store = SessionStore(tmp_path / ".pony" / "sessions")

    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=session_store,
        options=RuntimeOptions(project_trusted=True),
    )

    inputs = iter(["/help", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    run_repl(agent)
    out = capsys.readouterr().out
    assert HELP_DETAILS.strip().splitlines()[0] in out
