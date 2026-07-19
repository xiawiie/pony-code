import pytest
import json
import os
import signal

from pony.cli.app import main
from pony.cli.start import run_agent_once, run_repl
from pony.providers.transport import ProviderTransportError
from pony.state.session_store import UnsupportedLegacyEntry


CANARY = "hostile-config-canary-9f3d7a"


class _FailingAgent:
    def __init__(self, error):
        self.error = error

    def ask(self, _prompt):
        raise self.error

    def redact_text(self, text):
        return str(text).replace(CANARY, "<redacted>")


class _InterruptAgent:
    def __init__(self, *, signum=None):
        self.signum = signum
        self.finalized = 0

    def ask(self, _prompt):
        if self.signum is None:
            raise KeyboardInterrupt("stop")
        os.kill(os.getpid(), self.signum)
        raise AssertionError("signal handler did not interrupt")

    def redact_text(self, text):
        return str(text)

    def finalize_sandbox_session(self):
        self.finalized += 1
        return None


class _FinalizingFailureAgent(_FailingAgent):
    def __init__(self, error):
        super().__init__(error)
        self.finalized = 0

    def finalize_sandbox_session(self):
        self.finalized += 1
        return None


def test_one_shot_keyboard_interrupt_finalizes_sandbox_and_returns_130():
    agent = _InterruptAgent()

    assert run_agent_once(agent, ["finish"]) == 130
    assert agent.finalized == 0


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="SIGTERM unavailable")
def test_one_shot_sigterm_becomes_interrupt_and_restores_handler():
    agent = _InterruptAgent(signum=signal.SIGTERM)
    previous = signal.getsignal(signal.SIGTERM)

    assert run_agent_once(agent, ["finish"]) == 128 + signal.SIGTERM
    assert agent.finalized == 0
    assert signal.getsignal(signal.SIGTERM) is previous


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="SIGTERM unavailable")
def test_repl_sigterm_during_non_model_branch_still_finalizes(monkeypatch):
    agent = _InterruptAgent()
    agent.session_path = type(
        "SignalPath",
        (),
        {
            "__str__": lambda _self: (
                os.kill(os.getpid(), signal.SIGTERM),
                "unreachable",
            )[1]
        },
    )()
    inputs = iter(("/session",))
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

    assert run_repl(agent) == 128 + signal.SIGTERM
    assert agent.finalized == 0


@pytest.mark.parametrize("error_type", (OSError, ValueError))
def test_one_shot_contains_ordinary_runtime_failures(error_type, capsys):
    agent = _FailingAgent(error_type(f"finalization failed {CANARY}"))

    assert run_agent_once(agent, ["finish"]) == 1

    captured = capsys.readouterr()
    assert captured.err.strip() == "agent runtime failed"
    assert CANARY not in captured.out + captured.err


def test_one_shot_runtime_failure_still_finalizes_sandbox():
    agent = _FinalizingFailureAgent(ValueError("failed"))

    assert run_agent_once(agent, ["finish"]) == 1
    assert agent.finalized == 0


def _provider_failure():
    return ProviderTransportError(
        f"unsafe raw response {CANARY}",
        code="provider_protocol_mismatch",
        stage="tool_call",
        protocol_reason="tool_call_shape_invalid",
    )


def test_one_shot_provider_failure_is_rethrown_after_finalization():
    agent = _FinalizingFailureAgent(_provider_failure())

    with pytest.raises(ProviderTransportError):
        run_agent_once(agent, ["finish"])

    assert agent.finalized == 0


@pytest.mark.parametrize("output_format", ("text", "json"))
def test_cli_projects_provider_failure_without_raw_response(
    monkeypatch,
    capsys,
    output_format,
):
    agent = _FailingAgent(_provider_failure())
    agent.model_client = type(
        "BoundClient",
        (),
        {"provider_binding": {"protocol_family": "openai_chat_completions"}},
    )()
    monkeypatch.setattr("pony.cli.app.build_agent", lambda _args: agent)
    argv = ["--format", output_format, "run", "finish"]

    assert main(argv) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert CANARY not in combined
    if output_format == "json":
        error = json.loads(captured.out)["error"]
        assert error["code"] == "provider_protocol_mismatch"
        assert error["details"] == {
            "code": "provider_protocol_mismatch",
            "protocol": "openai_chat_completions",
            "reason": "tool_call_shape_invalid",
            "stage": "tool_call",
        }
    else:
        assert "Provider request failed" in captured.err
        assert "agent runtime failed" not in captured.err
        assert "openai_chat_completions" in captured.err


def test_plain_repl_provider_failure_propagates_after_finalization(monkeypatch):
    agent = _FinalizingFailureAgent(_provider_failure())
    agent.session = {}
    monkeypatch.setattr("builtins.input", lambda _prompt="": "finish")

    with pytest.raises(ProviderTransportError):
        run_repl(agent, plain=True)

    assert agent.finalized == 0


def test_cli_drops_unrecognized_provider_error_code(monkeypatch, capsys):
    failure = ProviderTransportError(
        "unsafe failure",
        code=f"unsafe_{CANARY}",
        stage="tool_call",
    )
    agent = _FailingAgent(failure)
    agent.model_client = type("BoundClient", (), {"provider_binding": {}})()
    monkeypatch.setattr("pony.cli.app.build_agent", lambda _args: agent)

    assert main(["--format", "json", "run", "finish"]) == 1

    output = capsys.readouterr().out
    assert CANARY not in output
    assert json.loads(output)["error"]["code"] == "provider_protocol_mismatch"


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

    monkeypatch.setattr("pony.cli.app.build_agent", fail_build)

    assert main(["--quiet", "run", "finish"]) == 5

    captured = capsys.readouterr()
    assert captured.err.strip() == "pony startup failed"
    assert CANARY not in captured.out + captured.err


def test_invalid_project_api_base_uses_safe_config_envelope(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=anthropic\n"
        "PONY_MODEL=claude-test\n"
        f"PONY_API_BASE=https://user:{CANARY}@example.com/v1\n"
        "PONY_API_KEY=test-key\n",
        encoding="utf-8",
    )

    assert main(["--cwd", str(tmp_path), "--quiet", "run", "hello"]) == 3

    captured = capsys.readouterr()
    assert captured.err.strip() == "api_base_credentials"
    assert CANARY not in captured.out + captured.err


def test_project_key_without_base_is_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=anthropic\n"
        "PONY_MODEL=claude-test\n"
        "PONY_API_KEY=stale-project-key\n",
        encoding="utf-8",
    )

    assert main(["--cwd", str(tmp_path), "--quiet", "run", "hello"]) == 3

    output = capsys.readouterr()
    assert output.err.splitlines()[0] == "api_base_not_configured"


def test_unsupported_legacy_session_uses_stable_json_error(
    tmp_path,
    monkeypatch,
    capsys,
):
    def reject_legacy(_args):
        raise UnsupportedLegacyEntry("model_change")

    monkeypatch.setattr("pony.cli.app.build_agent", reject_legacy)

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "--resume",
            "legacy",
            "repl",
        ]
    )

    assert code == 3
    output = capsys.readouterr().out
    assert '"code": "unsupported_legacy_entry"' in output
    assert "model_change" in output


def test_init_invalid_base_does_not_echo_input_value(tmp_path, monkeypatch, capsys):
    answers = iter(("", f"https://user:{CANARY}@example.com/v1"))
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    assert main(["--cwd", str(tmp_path), "init"]) == 3

    captured = capsys.readouterr()
    assert "api_base_credentials" in captured.err
    assert CANARY not in captured.out + captured.err
