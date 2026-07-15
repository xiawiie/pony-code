import getpass
import io
import json
import os
import stat

import pytest

from pico.cli import main
from pico.config import read_project_env


def _install_fake_agent(monkeypatch, tmp_path, called):
    def fake_build_agent(args):
        called["built"] = True
        called["prompt"] = list(getattr(args, "prompt", []))

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            approval_policy = "auto"
            session = {"id": "s"}
            session_path = str(tmp_path / ".pico" / "sessions" / "s.json")

            def ask(self, message):
                called["asked"] = message
                return "answer"

            def memory_text(self):
                return "memory"

            def reset(self):
                called["reset"] = True

        return FakeAgent()

    monkeypatch.setattr("pico.cli.build_agent", fake_build_agent)
    monkeypatch.setattr("pico.cli.build_welcome", lambda agent, model, host: "")


def test_run_command_calls_agent_once(tmp_path, monkeypatch, capsys):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    code = main(["--cwd", str(tmp_path), "run", "fix", "tests"])

    assert code == 0
    assert called["asked"] == "fix tests"
    assert "answer" in capsys.readouterr().out


def test_repl_command_exits_on_eof(tmp_path, monkeypatch):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError()))

    code = main(["--cwd", str(tmp_path), "repl"])

    assert code == 0
    assert called["built"] is True


@pytest.mark.parametrize(
    ("tokens", "usage"),
    [(["run"], "usage: pico run <prompt...>"), (["repl", "extra"], "usage: pico repl")],
)
def test_invalid_agent_command_arity_does_not_build_agent(
    tmp_path, monkeypatch, capsys, tokens, usage
):
    monkeypatch.setattr(
        "pico.cli.build_agent",
        lambda args: (_ for _ in ()).throw(AssertionError("must not build agent")),
    )

    code = main(["--cwd", str(tmp_path), *tokens])

    assert code == 2
    assert usage in capsys.readouterr().err


def test_bare_prompt_is_rejected_without_building_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "pico.cli.build_agent",
        lambda args: (_ for _ in ()).throw(AssertionError("must not build agent")),
    )

    code = main(["--cwd", str(tmp_path), "fix", "tests"])

    assert code == 2
    assert "Unknown command: fix" in capsys.readouterr().err


def test_help_command_shows_examples(capsys):
    code = main(["help"])

    assert code == 0
    out = capsys.readouterr().out
    assert "pico — Local coding agent" in out
    assert "USAGE:" in out
    assert "Available Commands:" in out
    assert "--sandbox    run shell tools" in out
    assert 'pico run "inspect the failing tests"' in out
    assert "pico config set-secret NAME [--stdin]" in out
    assert "pico --approval ask run" in out
    assert "pico checkpoints show <checkpoint-id>" in out
    assert "pico checkpoints pending" in out
    assert "pico runs summary latest" in out
    assert "migrate      Inspect and apply explicit artifact migrations" in out
    assert "Compatibility:" not in out
    assert "no OS sandbox" in out
    assert "providers list" not in out


def test_sandbox_flag_is_rejected_for_non_agent_commands(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        "pico.cli._dispatch_status",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )

    code = main(["--cwd", str(tmp_path), "--sandbox", "status"])

    assert code == 2
    assert "--sandbox is only valid" in capsys.readouterr().err


def test_help_flag_uses_root_help_without_argparse_dump(capsys):
    code = main(["--help"])

    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("pico — Local coding agent")
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
        "pico.cli.build_agent",
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


def test_init_creates_non_secret_project_env_without_building_agent(tmp_path, monkeypatch, capsys):
    def fail_build_agent(args):
        raise AssertionError("init must not build a Pico agent")

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)

    code = main([
        "--cwd",
        str(tmp_path),
        "init",
        "--provider",
        "deepseek",
    ])

    assert code == 0
    values = read_project_env(tmp_path, warn=False)
    assert values["PICO_PROVIDER"] == "deepseek"
    assert values["PICO_DEEPSEEK_MODEL"] == "deepseek-v4-pro"
    assert values["PICO_DEEPSEEK_API_BASE"] == "https://api.deepseek.com/anthropic"
    assert "PICO_DEEPSEEK_API_KEY" not in values

    out = capsys.readouterr().out
    assert out.startswith("Pico init")
    assert "Workspace" in out
    assert str(tmp_path.resolve()) in out
    assert "Project environment" in out
    assert "repo_root_exact" in out
    assert "loaded" in out
    assert "pico config set-secret PICO_DEEPSEEK_API_KEY" in out

    code = main(["--cwd", str(tmp_path), "--format", "json", "config", "show"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["provider"]["value"] == "deepseek"
    assert payload["data"]["api_key"] == {
        "present": False,
        "source": "unset",
        "name": "",
    }


def test_init_updates_existing_env_without_dropping_unrelated_lines(tmp_path, capsys):
    (tmp_path / ".env").write_text(
        "# keep this comment\n"
        "OTHER_SETTING=kept\n"
        "PICO_PROVIDER=deepseek\n"
        "PICO_DEEPSEEK_API_KEY=old-secret\n",
        encoding="utf-8",
    )

    code = main([
        "--cwd",
        str(tmp_path),
        "init",
        "--provider",
        "openai",
        "--model",
        "gpt-project",
    ])

    assert code == 0
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "# keep this comment\n" in env_text
    assert "OTHER_SETTING=kept\n" in env_text
    values = read_project_env(tmp_path, warn=False)
    assert values["PICO_PROVIDER"] == "openai"
    assert values["PICO_OPENAI_MODEL"] == "gpt-project"
    assert values["PICO_OPENAI_API_BASE"] == "https://www.right.codes/codex/v1"
    assert values["PICO_DEEPSEEK_API_KEY"] == "old-secret"
    assert "PICO_OPENAI_API_KEY" not in values

    out = capsys.readouterr().out
    assert "updated" in out
    assert "old-secret" not in out


def test_init_json_reports_missing_api_key_without_prompting(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        getpass,
        "getpass",
        lambda prompt: (_ for _ in ()).throw(AssertionError("getpass called")),
    )
    code = main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "init",
        "--provider",
        "anthropic",
    ])

    assert code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["kind"] == "config_init"
    assert payload["data"]["provider"] == "anthropic"
    assert payload["data"]["api_key"] == {
        "present": False,
        "name": "PICO_ANTHROPIC_API_KEY",
    }
    assert "PICO_ANTHROPIC_API_KEY" in output


def test_init_preserves_existing_ollama_host_when_cli_host_is_default(tmp_path):
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=ollama\n"
        "PICO_OLLAMA_HOST=http://ollama.example:11434\n",
        encoding="utf-8",
    )

    code = main(["--cwd", str(tmp_path), "init", "--provider", "ollama"])

    assert code == 0
    values = read_project_env(tmp_path, warn=False)
    assert values["PICO_PROVIDER"] == "ollama"
    assert values["PICO_OLLAMA_HOST"] == "http://ollama.example:11434"


@pytest.mark.parametrize("flag", ("--api-key", "--api-key="))
def test_init_rejects_api_key_argv_without_echoing_value(tmp_path, flag, capsys):
    secret = "sk-cli-secret-123456789"
    argv = ["--cwd", str(tmp_path), "init", flag]
    if flag == "--api-key":
        argv.append(secret)
    else:
        argv[-1] += secret

    code = main(argv)

    captured = capsys.readouterr()
    assert code == 2
    assert secret not in captured.out + captured.err
    assert not (tmp_path / ".env").exists()


def test_config_set_secret_reads_stdin_and_writes_private_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-stdin-secret-123456789\n"))

    code = main([
        "--cwd",
        str(tmp_path),
        "config",
        "set-secret",
        "PICO_DEEPSEEK_API_KEY",
        "--stdin",
    ])

    assert code == 0
    assert read_project_env(tmp_path, warn=False)["PICO_DEEPSEEK_API_KEY"] == "sk-stdin-secret-123456789"
    captured = capsys.readouterr()
    assert "sk-stdin" not in captured.out + captured.err
    assert "Workspace" in captured.out
    assert str(tmp_path.resolve()) in captured.out
    assert "Project environment" in captured.out
    assert "repo_root_exact" in captured.out
    assert "loaded" in captured.out
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600


def test_config_set_secret_uses_getpass_without_rendering_value(tmp_path, monkeypatch, capsys):
    secret = "sk-getpass-secret-123456789"
    monkeypatch.setattr(getpass, "getpass", lambda prompt: secret)

    code = main([
        "--cwd",
        str(tmp_path),
        "config",
        "set-secret",
        "PICO_DEEPSEEK_API_KEY",
    ])

    captured = capsys.readouterr()
    assert code == 0
    assert read_project_env(tmp_path, warn=False)["PICO_DEEPSEEK_API_KEY"] == secret
    assert secret not in captured.out + captured.err


def test_config_set_secret_storage_failure_is_stable(tmp_path, monkeypatch, capsys):
    marker = "sk-sensitive-lock-path-123456789"
    outside = tmp_path.parent / marker
    outside.mkdir()
    (tmp_path / ".pico").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-input-secret-123456789\n"))

    code = main([
        "--cwd",
        str(tmp_path),
        "config",
        "set-secret",
        "PICO_DEEPSEEK_API_KEY",
        "--stdin",
    ])

    captured = capsys.readouterr()
    assert code == 3
    assert marker not in captured.out + captured.err
    assert "project environment update failed" in captured.out + captured.err


def test_init_writes_only_non_secret_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(
        getpass,
        "getpass",
        lambda prompt: (_ for _ in ()).throw(AssertionError("getpass called")),
    )

    code = main(["--cwd", str(tmp_path), "init", "--provider", "deepseek"])

    assert code == 0
    values = read_project_env(tmp_path, warn=False)
    assert values["PICO_PROVIDER"] == "deepseek"
    assert "PICO_DEEPSEEK_API_KEY" not in values


def test_config_write_output_uses_canonical_project_env_metadata(
    tmp_path,
    monkeypatch,
    capsys,
):
    root = tmp_path / "repo"
    root.mkdir()

    assert main([
        "--cwd",
        str(root),
        "--format",
        "json",
        "init",
        "--provider",
        "deepseek",
    ]) == 0
    init_payload = json.loads(capsys.readouterr().out)["data"]
    assert init_payload["workspace"] == {
        "repo_root": str(root.resolve()),
    }
    assert init_payload["project_env"] == {
        "path": str(root.resolve() / ".env"),
        "scope": "repo_root_exact",
        "status": "loaded",
    }
    assert "env_path" not in init_payload

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("sk-output-test-secret-123456789\n"),
    )
    assert main([
        "--cwd",
        str(root),
        "--format",
        "json",
        "config",
        "set-secret",
        "PICO_DEEPSEEK_API_KEY",
        "--stdin",
    ]) == 0
    secret_payload = json.loads(capsys.readouterr().out)["data"]
    assert secret_payload["workspace"] == {
        "repo_root": str(root.resolve()),
    }
    assert secret_payload["project_env"] == {
        "path": str(root.resolve() / ".env"),
        "scope": "repo_root_exact",
        "status": "loaded",
    }
    assert "env_path" not in secret_payload


def test_config_writes_redact_secret_shaped_workspace_in_project_env_path(
    tmp_path,
    monkeypatch,
    capsys,
):
    marker = "sk-workspace-path-123456789"
    root = tmp_path / marker
    root.mkdir()

    assert main([
        "--cwd",
        str(root),
        "--format",
        "json",
        "init",
        "--provider",
        "deepseek",
    ]) == 0
    init_output = capsys.readouterr().out
    init_data = json.loads(init_output)["data"]
    assert set(init_data["workspace"]) == {"repo_root"}
    assert init_data["project_env"]["scope"] == "repo_root_exact"
    assert init_data["project_env"]["status"] == "loaded"
    assert marker not in init_output

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("sk-output-test-secret-123456789\n"),
    )
    assert main([
        "--cwd",
        str(root),
        "--format",
        "json",
        "config",
        "set-secret",
        "PICO_DEEPSEEK_API_KEY",
        "--stdin",
    ]) == 0
    secret_output = capsys.readouterr().out
    secret_data = json.loads(secret_output)["data"]
    assert set(secret_data["workspace"]) == {"repo_root"}
    assert secret_data["project_env"]["scope"] == "repo_root_exact"
    assert secret_data["project_env"]["status"] == "loaded"
    assert marker not in secret_output
    assert "sk-output-test-secret" not in secret_output


def test_config_writes_keep_review_required_for_preserved_invalid_line(
    tmp_path,
    monkeypatch,
    capsys,
):
    marker = "sk-" + "preserved-invalid-line-123456789"
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"PICO_PROVIDER=deepseek\n{marker}\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "init",
        "--provider",
        "deepseek",
    ]) == 0
    init_capture = capsys.readouterr()
    init_data = json.loads(init_capture.out)["data"]
    assert init_data["project_env"]["status"] == "review_required"
    assert marker not in init_capture.out + init_capture.err

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("sk-output-test-secret-123456789\n"),
    )
    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "config",
        "set-secret",
        "PICO_DEEPSEEK_API_KEY",
        "--stdin",
    ]) == 0
    secret_capture = capsys.readouterr()
    secret_data = json.loads(secret_capture.out)["data"]
    assert secret_data["project_env"]["status"] == "review_required"
    assert marker not in secret_capture.out + secret_capture.err
    assert "sk-output-test-secret" not in secret_capture.out + secret_capture.err
    assert marker in env_path.read_text(encoding="utf-8")


def test_repl_help_renders_help_details(tmp_path, monkeypatch, capsys):
    from pico.cli_help import HELP_DETAILS
    from pico.cli_start import run_repl
    from pico.providers.fake import FakeModelClient
    from pico.runtime import Pico
    from pico.session_store import SessionStore
    from pico.workspace import WorkspaceContext

    workspace = WorkspaceContext.build(tmp_path)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    session_store = SessionStore(tmp_path / ".pico" / "sessions")

    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=session_store,
        approval_policy="auto",
    )

    inputs = iter(["/help", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    run_repl(agent)
    out = capsys.readouterr().out
    assert HELP_DETAILS.strip().splitlines()[0] in out
