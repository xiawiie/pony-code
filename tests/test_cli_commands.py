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


@pytest.mark.parametrize(
    "argv",
    [
        ["--" + "provider", "openai", "run", "hi"],
        ["--model", "qwen3.5:4b", "run", "hi"],
        ["--base-url", "https://example.test/v1", "run", "hi"],
    ],
)
def test_removed_root_model_flags_are_usage_errors_without_building_agent(
    tmp_path,
    monkeypatch,
    capsys,
    argv,
):
    def fail_build_agent(args):
        raise AssertionError("removed root model flags must not build a Pico agent")

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)

    code = main(["--cwd", str(tmp_path), *argv])

    assert code == 2
    captured = capsys.readouterr()
    assert "removed option" in captured.err
    assert argv[0] in captured.err
    assert "answer" not in captured.out


@pytest.mark.parametrize(
    ("argv", "option"),
    [
        (
            ["--model-base-url", "https://api.example.test/v1", "run", "hi"],
            "--model-base-url",
        ),
        (
            ["run", "--model-base-url", "https://api.example.test/v1", "hi"],
            "--model-base-url",
        ),
        (
            ["--model-api", "openai-responses", "run", "hi"],
            "--model-api",
        ),
        (
            ["run", "--model-api=openai-responses", "hi"],
            "--model-api",
        ),
        (
            ["run", "--api-key-env", "OPENAI_API_KEY", "hi"],
            "--api-key-env",
        ),
        (
            ["run", "--model-api-key-env=OPENAI_API_KEY", "hi"],
            "--model-api-key-env",
        ),
    ],
)
def test_init_only_model_flags_are_usage_errors_outside_init(
    tmp_path,
    monkeypatch,
    capsys,
    argv,
    option,
):
    def fail_build_agent(args):
        raise AssertionError("init-only model flags must not build a Pico agent")

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)

    code = main(["--cwd", str(tmp_path), *argv])

    assert code == 2
    captured = capsys.readouterr()
    assert f"init-only option: {option}" in captured.err
    assert "pico-cli init" in captured.err
    assert "answer" not in captured.out


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


def test_init_creates_model_config_without_building_agent(tmp_path, monkeypatch, capsys):
    def fail_build_agent(args):
        raise AssertionError("init must not build a Pico agent")

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)

    code = main([
        "--cwd",
        str(tmp_path),
        "init",
        "--model",
        "deepseek-chat",
        "--base-url",
        "https://api.deepseek.com/anthropic",
        "--api-key-env",
        "DEEPSEEK_API_KEY",
        "--api-key",
        "sk-project-deepseek",
        "--api",
        "anthropic-messages",
    ])

    assert code == 0
    pico_toml = (tmp_path / "pico.toml").read_text(encoding="utf-8")
    assert pico_toml == (
        "[model]\n"
        'name = "deepseek-chat"\n'
        'base_url = "https://api.deepseek.com/anthropic"\n'
        'api_key_env = "DEEPSEEK_API_KEY"\n'
        'api = "anthropic-messages"\n'
    )
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=sk-project-deepseek\n" in env_text

    out = capsys.readouterr().out
    assert out.startswith("Pico init")
    assert "sk-project-deepseek" not in out

    code = main(["--cwd", str(tmp_path), "--format", "json", "config", "show"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["model"]["status"] == "ok"
    assert payload["data"]["model"]["name"] == "deepseek-chat"
    assert payload["data"]["model"]["base_url"] == "https://api.deepseek.com/anthropic"
    assert payload["data"]["model"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert payload["data"]["model"]["api_key_present"] is True
    assert payload["data"]["model"]["api"] == "anthropic-messages"
    assert payload["data"]["model"]["adapter"] == "AnthropicMessagesAdapter"


def test_init_accepts_model_prefixed_config_flags(tmp_path, monkeypatch):
    def fail_build_agent(args):
        raise AssertionError("init must not build a Pico agent")

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)

    code = main([
        "--cwd",
        str(tmp_path),
        "init",
        "--model",
        "gpt-5.4",
        "--model-base-url",
        "https://api.openai.com/v1",
        "--model-api-key-env",
        "OPENAI_API_KEY",
        "--model-api",
        "openai-responses",
    ])

    assert code == 0
    assert (tmp_path / "pico.toml").read_text(encoding="utf-8") == (
        "[model]\n"
        'name = "gpt-5.4"\n'
        'base_url = "https://api.openai.com/v1"\n'
        'api_key_env = "OPENAI_API_KEY"\n'
        'api = "openai-responses"\n'
    )


def test_init_updates_existing_env_without_dropping_unrelated_lines(tmp_path, capsys):
    (tmp_path / ".env").write_text(
        "# keep this comment\n"
        "OTHER_SETTING=kept\n"
        "MODEL_API_KEY=old-secret\n",
        encoding="utf-8",
    )

    code = main([
        "--cwd",
        str(tmp_path),
        "init",
        "--model",
        "gpt-project",
        "--base-url",
        "https://www.right.codes/codex/v1",
        "--api-key-env",
        "MODEL_API_KEY",
        "--api-key",
        "sk-openai-project",
    ])

    assert code == 0
    pico_toml = (tmp_path / "pico.toml").read_text(encoding="utf-8")
    assert 'name = "gpt-project"\n' in pico_toml
    assert 'base_url = "https://www.right.codes/codex/v1"\n' in pico_toml
    assert 'api_key_env = "MODEL_API_KEY"\n' in pico_toml
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "# keep this comment\n" in env_text
    assert "OTHER_SETTING=kept\n" in env_text
    assert "MODEL_API_KEY=sk-openai-project\n" in env_text

    out = capsys.readouterr().out
    assert "updated" in out
    assert "sk-openai-project" not in out


def test_init_updates_model_section_without_dropping_other_pico_toml_sections(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "# project config\n"
        "[policy]\n"
        "max_blob_size = 2048\n"
        "\n"
        "[model]\n"
        'name = "old-model"\n'
        'base_url = "https://old.example.test/v1"\n'
        "\n"
        "[context]\n"
        "history_soft_cap = 1234\n",
        encoding="utf-8",
    )

    code = main([
        "--cwd",
        str(tmp_path),
        "init",
        "--model",
        "qwen-max",
        "--base-url",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "--api-key-env",
        "DASHSCOPE_API_KEY",
        "--api",
        "openai-chat",
    ])

    assert code == 0
    pico_toml = (tmp_path / "pico.toml").read_text(encoding="utf-8")
    assert "# project config\n" in pico_toml
    assert "[policy]\nmax_blob_size = 2048\n" in pico_toml
    assert "[context]\nhistory_soft_cap = 1234\n" in pico_toml
    assert 'name = "old-model"\n' not in pico_toml
    assert 'base_url = "https://old.example.test/v1"\n' not in pico_toml
    assert (
        "[model]\n"
        'name = "qwen-max"\n'
        'base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"\n'
        'api_key_env = "DASHSCOPE_API_KEY"\n'
        'api = "openai-chat"\n'
    ) in pico_toml


def test_init_replaces_model_section_without_dropping_following_array_table(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[model]\n"
        'name = "old-model"\n'
        'base_url = "https://old.example.test/v1"\n'
        "\n"
        "[[profiles]]\n"
        'name = "ci"\n'
        "max_steps = 4\n",
        encoding="utf-8",
    )

    code = main([
        "--cwd",
        str(tmp_path),
        "init",
        "--model",
        "qwen-max",
        "--base-url",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ])

    assert code == 0
    pico_toml = (tmp_path / "pico.toml").read_text(encoding="utf-8")
    assert 'name = "old-model"\n' not in pico_toml
    assert 'base_url = "https://old.example.test/v1"\n' not in pico_toml
    assert "[[profiles]]\n" in pico_toml
    assert 'name = "ci"\n' in pico_toml
    assert "max_steps = 4\n" in pico_toml


@pytest.mark.parametrize(
    "tokens",
    [
        [
            "--model",
            "qwen-max",
            "--base-url",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "--api-key-env",
            "BAD-NAME",
        ],
        [
            "--model",
            "qwen-max",
            "--base-url",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "--api-key",
            "sk-without-env",
        ],
        [
            "--model",
            "qwen-max",
            "--base-url",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "--api-key-env",
            "DASHSCOPE_API_KEY",
            "--api-key",
            "",
        ],
    ],
)
def test_init_rejects_unusable_api_key_configuration(tmp_path, tokens):
    code = main(["--cwd", str(tmp_path), "init", *tokens])

    assert code == 2
    assert not (tmp_path / "pico.toml").exists()
    assert not (tmp_path / ".env").exists()


def test_init_malformed_existing_env_returns_usage_without_changing_pico_toml(
    tmp_path,
):
    original_pico_toml = "[context]\nhistory_soft_cap = 1234\n"
    (tmp_path / "pico.toml").write_text(original_pico_toml, encoding="utf-8")
    (tmp_path / ".env").write_text("MALFORMED\n", encoding="utf-8")

    code = main([
        "--cwd",
        str(tmp_path),
        "init",
        "--model",
        "qwen-max",
        "--base-url",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "--api-key-env",
        "DASHSCOPE_API_KEY",
        "--api-key",
        "sk-project",
    ])

    assert code == 2
    assert (tmp_path / "pico.toml").read_text(encoding="utf-8") == original_pico_toml


def test_init_api_key_with_newline_fails_without_writing_pico_toml(tmp_path):
    code = main([
        "--cwd",
        str(tmp_path),
        "init",
        "--model",
        "qwen-max",
        "--base-url",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "--api-key-env",
        "DASHSCOPE_API_KEY",
        "--api-key",
        "sk-project\nsecret",
    ])

    assert code == 2
    assert not (tmp_path / "pico.toml").exists()
    assert not (tmp_path / ".env").exists()


def test_init_json_redacts_api_key_value(tmp_path, capsys):
    code = main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "init",
        "--model",
        "claude-sonnet-4-6",
        "--base-url",
        "https://www.right.codes/claude/v1",
        "--api-key-env",
        "ANTHROPIC_API_KEY",
        "--api-key",
        "sk-anthropic-project",
        "--api",
        "anthropic-messages",
    ])

    assert code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["kind"] == "config_init"
    assert payload["data"]["model"] == "claude-sonnet-4-6"
    assert payload["data"]["base_url"] == "https://www.right.codes/claude/v1"
    assert payload["data"]["api"] == "anthropic-messages"
    assert payload["data"]["api_key"] == {
        "present": True,
        "name": "ANTHROPIC_API_KEY",
    }
    assert "sk-anthropic-project" not in output


@pytest.mark.parametrize("format_args", [[], ["--format", "json"]])
def test_init_redacts_secret_base_url_in_output_but_stores_raw(
    tmp_path,
    capsys,
    format_args,
):
    base_url = "https://user:pass@example.com/v1?token=secret#frag"

    code = main([
        "--cwd",
        str(tmp_path),
        *format_args,
        "init",
        "--model",
        "custom-model",
        "--base-url",
        base_url,
        "--api",
        "openai-chat",
    ])

    assert code == 0
    output = capsys.readouterr().out
    assert "user:pass" not in output
    assert "pass@example" not in output
    assert "token=secret" not in output
    assert "#frag" not in output
    assert 'base_url = "https://user:pass@example.com/v1?token=secret#frag"\n' in (
        tmp_path / "pico.toml"
    ).read_text(encoding="utf-8")
    if format_args:
        payload = json.loads(output)
        assert payload["data"]["base_url"] == "https://example.com/v1"
    else:
        assert "https://example.com/v1" in output


def test_init_without_api_key_env_does_not_write_env_file(tmp_path):
    code = main([
        "--cwd",
        str(tmp_path),
        "init",
        "--model",
        "qwen3.5:4b",
        "--base-url",
        "http://127.0.0.1:11434",
    ])

    assert code == 0
    assert (tmp_path / "pico.toml").read_text(encoding="utf-8") == (
        "[model]\n"
        'name = "qwen3.5:4b"\n'
        'base_url = "http://127.0.0.1:11434"\n'
    )
    assert not (tmp_path / ".env").exists()


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

        def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
            from pico.providers.response import Response, StopReason

            return Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "<final>ok</final>"}],
                usage={},
            )

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
