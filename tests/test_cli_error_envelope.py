import pytest

from pico.cli import main
from pico.cli_start import run_agent_once, run_repl


CANARY = "hostile-config-canary-9f3d7a"


class _FailingAgent:
    def __init__(self, error):
        self.error = error

    def ask(self, _prompt):
        raise self.error

    def redact_text(self, text):
        return str(text).replace(CANARY, "<redacted>")


@pytest.mark.parametrize("error_type", (OSError, ValueError))
def test_one_shot_contains_ordinary_runtime_failures(error_type, capsys):
    agent = _FailingAgent(error_type(f"finalization failed {CANARY}"))

    assert run_agent_once(agent, ["finish"]) == 1

    captured = capsys.readouterr()
    assert captured.err.strip() == "agent runtime failed"
    assert CANARY not in captured.out + captured.err


@pytest.mark.parametrize("error_type", (OSError, ValueError))
def test_repl_contains_ordinary_runtime_failures(
    error_type,
    monkeypatch,
    capsys,
):
    agent = _FailingAgent(error_type(f"finalization failed {CANARY}"))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "finish")

    assert run_repl(agent) == 1

    captured = capsys.readouterr()
    assert captured.err.strip() == "agent runtime failed"
    assert CANARY not in captured.out + captured.err


def test_cli_contains_startup_io_failure(monkeypatch, capsys):
    def fail_build(_args):
        raise OSError(f"failed to open /private/{CANARY}")

    monkeypatch.setattr("pico.cli.build_agent", fail_build)

    assert main(["--quiet", "run", "finish"]) == 5

    captured = capsys.readouterr()
    assert captured.err.strip() == "pico startup failed"
    assert CANARY not in captured.out + captured.err


def test_cli_contains_welcome_render_failure(monkeypatch, capsys):
    agent = type(
        "Agent",
        (),
        {"model_client": type("Model", (), {"model": "safe"})()},
    )()
    monkeypatch.setattr("pico.cli.build_agent", lambda _args: agent)

    def fail_welcome(*_args, **_kwargs):
        raise ValueError(f"bad workspace path {CANARY}")

    monkeypatch.setattr("pico.cli.build_welcome", fail_welcome)

    assert main(["run", "finish"]) == 5

    captured = capsys.readouterr()
    assert captured.err.strip() == "pico startup failed"
    assert CANARY not in captured.out + captured.err


def test_invalid_project_provider_uses_safe_config_envelope(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("PICO_PROVIDER", "ollama")
    (tmp_path / ".env").write_text(
        f"PICO_PROVIDER={CANARY}\n",
        encoding="utf-8",
    )

    assert main(["--cwd", str(tmp_path), "--quiet", "run", "hello"]) == 3

    captured = capsys.readouterr()
    assert captured.err.strip() == "invalid provider configuration"
    assert CANARY not in captured.out + captured.err


def test_init_invalid_provider_does_not_echo_project_value(tmp_path, capsys):
    (tmp_path / ".env").write_text(
        f"PICO_PROVIDER={CANARY}\n",
        encoding="utf-8",
    )
    # Exercise the real pre-agent command path, not the model-client builder.
    assert main(["--cwd", str(tmp_path), "init"]) == 2

    captured = capsys.readouterr()
    assert "unknown provider" in captured.err
    assert CANARY not in captured.out + captured.err
