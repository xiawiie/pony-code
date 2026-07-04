import json

import pytest

from pico.cli import main


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


def test_legacy_prompt_remains_silent_compatibility(tmp_path, monkeypatch, capsys):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    code = main(["--cwd", str(tmp_path), "fix", "tests"])

    assert code == 0
    out = capsys.readouterr().out
    assert "answer" in out
    assert "pico run" not in out


def test_help_command_shows_examples(capsys):
    code = main(["help"])

    assert code == 0
    out = capsys.readouterr().out
    assert "pico-cli — Local coding agent" in out
    assert "USAGE:" in out
    assert "Available Commands:" in out
    assert 'pico-cli run "inspect the failing tests"' in out
    assert "providers list" not in out


def test_help_flag_uses_root_help_without_argparse_dump(capsys):
    code = main(["--help"])

    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("pico-cli — Local coding agent")
    assert "Available Commands:" in out
    assert "positional arguments:" not in out


def test_unknown_command_suggests_close_match(capsys):
    code = main(["chekpoints", "list"])

    assert code == 2
    err = capsys.readouterr().err
    assert "Unknown command: chekpoints" in err
    assert "Did you mean `checkpoints`?" in err


@pytest.mark.parametrize(
    ("tokens", "prompt"),
    [
        (["hello"], "hello"),
        (["start", "a", "project"], "start a project"),
        (["running", "tests"], "running tests"),
        (["check", "tests"], "check tests"),
    ],
)
def test_natural_language_legacy_prompts_are_not_command_typos(
    tmp_path,
    monkeypatch,
    tokens,
    prompt,
):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    code = main(["--cwd", str(tmp_path), *tokens])

    assert code == 0
    assert called["asked"] == prompt


def test_unknown_command_suggestion_uses_json_error_envelope(capsys):
    code = main(["--format", "json", "chekpoints", "list"])

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unknown_command"
    assert payload["error"]["message"] == "Unknown command: chekpoints"
    assert payload["error"]["hint"] == "Did you mean `checkpoints`?"


def test_cli_command_specs_drive_namespace_tables():
    from pico import cli

    expected_namespaces = {
        name: spec["subcommands"]
        for name, spec in cli.COMMAND_SPECS.items()
        if spec["subcommands"]
    }

    assert cli._COMMAND_NAMESPACE_SUBCOMMANDS == expected_namespaces
    assert cli._RECOVERY_TOP_LEVEL_COMMANDS == {
        name
        for name, spec in cli.COMMAND_SPECS.items()
        if spec["category"] == "recovery"
    }


def test_init_creates_project_env_without_building_agent(tmp_path, monkeypatch, capsys):
    def fail_build_agent(args):
        raise AssertionError("init must not build a Pico agent")

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)

    code = main([
        "--cwd",
        str(tmp_path),
        "init",
        "--provider",
        "deepseek",
        "--api-key",
        "sk-project-deepseek",
    ])

    assert code == 0
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "PICO_PROVIDER=deepseek\n" in env_text
    assert "PICO_DEEPSEEK_MODEL=deepseek-v4-pro\n" in env_text
    assert "PICO_DEEPSEEK_API_BASE=https://api.deepseek.com/anthropic\n" in env_text
    assert "PICO_DEEPSEEK_API_KEY=sk-project-deepseek\n" in env_text

    out = capsys.readouterr().out
    assert out.startswith("Pico init")
    assert "sk-project-deepseek" not in out

    code = main(["--cwd", str(tmp_path), "--format", "json", "config", "show"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["provider"]["value"] == "deepseek"
    assert payload["data"]["api_key"] == {
        "present": True,
        "source": "project_env",
        "name": "PICO_DEEPSEEK_API_KEY",
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
        "--api-key",
        "sk-openai-project",
    ])

    assert code == 0
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "# keep this comment\n" in env_text
    assert "OTHER_SETTING=kept\n" in env_text
    assert "PICO_PROVIDER=openai\n" in env_text
    assert "PICO_OPENAI_MODEL=gpt-project\n" in env_text
    assert "PICO_OPENAI_API_BASE=https://www.right.codes/codex/v1\n" in env_text
    assert "PICO_OPENAI_API_KEY=sk-openai-project\n" in env_text

    out = capsys.readouterr().out
    assert "updated" in out
    assert "sk-openai-project" not in out


def test_init_json_redacts_api_key_value(tmp_path, capsys):
    code = main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "init",
        "--provider",
        "anthropic",
        "--api-key",
        "sk-anthropic-project",
    ])

    assert code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["kind"] == "config_init"
    assert payload["data"]["provider"] == "anthropic"
    assert payload["data"]["api_key"] == {
        "present": True,
        "name": "PICO_ANTHROPIC_API_KEY",
    }
    assert "sk-anthropic-project" not in output


def test_init_preserves_existing_ollama_host_when_cli_host_is_default(tmp_path):
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=ollama\n"
        "PICO_OLLAMA_HOST=http://ollama.example:11434\n",
        encoding="utf-8",
    )

    code = main(["--cwd", str(tmp_path), "init", "--provider", "ollama"])

    assert code == 0
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "PICO_PROVIDER=ollama\n" in env_text
    assert "PICO_OLLAMA_HOST=http://ollama.example:11434\n" in env_text


def test_repl_help_renders_help_details(tmp_path, monkeypatch, capsys):
    from pico.cli import HELP_DETAILS
    from pico.cli_commands import run_repl
    from pico.runtime import Pico, SessionStore
    from pico.workspace import WorkspaceContext

    workspace = WorkspaceContext.build(tmp_path)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    session_store = SessionStore(tmp_path / ".pico" / "sessions")

    class _FakeModel:
        supports_prompt_cache = False
        last_completion_metadata = {}

        def complete(self, prompt, max_new_tokens, **kwargs):
            return "<final>ok</final>"

        def stream_complete(self, *args, **kwargs):
            return self.complete(*args, **kwargs)

    agent = Pico(
        model_client=_FakeModel(),
        workspace=workspace,
        session_store=session_store,
        approval_policy="auto",
    )

    inputs = iter(["/help", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    run_repl(agent)
    out = capsys.readouterr().out
    assert HELP_DETAILS.strip().splitlines()[0] in out
