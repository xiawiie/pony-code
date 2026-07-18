import getpass
import io
import json
import os
import stat

import pytest

from pony.cli.app import main
from pony.config.environment import read_project_env
from pony.runtime.options import RuntimeOptions


def _install_fake_agent(monkeypatch, tmp_path, called):
    def fake_build_agent(args):
        called["built"] = True
        called["prompt"] = list(getattr(args, "prompt", []))

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            approval_policy = "auto"
            session = {"id": "s"}
            session_path = str(tmp_path / ".pony" / "sessions" / "s.json")

            def ask(self, message):
                called["asked"] = message
                return "answer"

            def set_workflow_mode(self, mode):
                called["mode"] = mode

            def memory_text(self):
                return "memory"

            def reset(self):
                called["reset"] = True

        return FakeAgent()

    monkeypatch.setattr("pony.cli.app.build_agent", fake_build_agent)


def test_run_command_calls_agent_once(tmp_path, monkeypatch, capsys):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    code = main(["--cwd", str(tmp_path), "run", "fix", "tests"])

    assert code == 0
    assert called["asked"] == "fix tests"
    assert "answer" in capsys.readouterr().out


def test_run_mode_is_applied_after_runtime_build(tmp_path, monkeypatch):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    assert main(["--cwd", str(tmp_path), "--mode", "plan", "run", "inspect"]) == 0
    assert called["mode"] == "plan"


def test_mode_is_rejected_for_management_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "pony.cli.app.build_agent",
        lambda _args: pytest.fail("agent must not be built"),
    )

    assert main(["--cwd", str(tmp_path), "--mode", "review", "status"]) == 2
    assert "--mode is only valid" in capsys.readouterr().err


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
    assert "--sandbox    run/repl in local Docker Sandbox (macOS arm64 only)" in out
    assert 'pony run "inspect the failing tests"' in out
    assert "pony config set-secret PONY_API_KEY" in out
    assert "pony --approval ask run" in out
    assert "pony checkpoints show <checkpoint-id>" in out
    assert "pony checkpoints pending" in out
    assert "pony runs summary latest" in out
    assert "migrate      Inspect and apply explicit artifact migrations" in out
    assert "Compatibility:" not in out
    assert "no OS sandbox" in out
    assert "all model-visible file tools use filtered" in out
    assert "providers list" not in out


def test_sandbox_flag_is_rejected_for_non_agent_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "pony.cli.app._dispatch_status",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )

    code = main(["--cwd", str(tmp_path), "--sandbox", "status"])

    assert code == 2
    assert "--sandbox is only valid" in capsys.readouterr().err


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
    provider="",
    api_base="",
    model="",
    key="test-key",
):
    answers = iter((provider, api_base, model))
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda prompt: key)


def test_init_prompts_for_url_and_hidden_key_without_building_agent_or_network(
    tmp_path, monkeypatch, capsys
):
    _install_init_input(monkeypatch)
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
        "PONY_PROVIDER": "anthropic",
        "PONY_API_BASE": "https://api.anthropic.com/v1",
        "PONY_MODEL": "claude-sonnet-4-6",
        "PONY_API_KEY": "test-key",
    }
    captured = capsys.readouterr()
    assert "Provider [anthropic]:" in captured.err
    assert "API Base [https://api.anthropic.com/v1]:" in captured.err
    assert captured.out.startswith("Pony init")
    assert "claude-sonnet-4-6" in captured.out
    assert "anthropic_messages" in captured.out
    assert "test-key" not in captured.out + captured.err


def test_init_accepts_exact_third_party_api_base(tmp_path, monkeypatch, capsys):
    _install_init_input(
        monkeypatch,
        provider="openai",
        api_base="https://lumina.tripo3d.com/v1/",
        key="gateway-key",
    )

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
    assert payload["provider"] == "openai"
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

    assert main(["--cwd", str(tmp_path), "init"]) == 0

    assert read_project_env(tmp_path, warn=False) == {
        "PONY_PROVIDER": "openai",
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
        "PONY_API_BASE=https://old.example/v1\nPONY_API_KEY=existing-key\n",
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
        "PONY_API_BASE=https://old.example/v1\n"
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

    assert main(["--cwd", str(tmp_path), "init"]) == 2

    assert "API Key is required unless auth mode is none" in capsys.readouterr().err
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
        ["--model", "other-model"],
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
        options=RuntimeOptions(approval_policy="auto"),
    )

    inputs = iter(["/help", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    run_repl(agent)
    out = capsys.readouterr().out
    assert HELP_DETAILS.strip().splitlines()[0] in out
