import pytest
import os
import signal

from pony.cli.app import main
from pony.cli.start import run_agent_once, run_repl


CANARY = "hostile-config-canary-9f3d7a"


class _FailingAgent:
    def __init__(self, error):
        self.error = error

    def ask(self, _prompt):
        raise self.error

    def redact_text(self, text):
        return str(text).replace(CANARY, "<redacted>")


class _ReviewAgent:
    def ask(self, _prompt):
        return "done"

    def redact_text(self, text):
        return str(text)

    def finalize_sandbox_session(self):
        return {
            "status": "diff_blocked",
            "sandbox_id": "sandbox_" + "1" * 32,
            "session_state": "pending_review",
            "generated_count": 4,
            "artifact": {
                "counts": {
                    "candidate": 2,
                    "high_risk_candidate": 1,
                    "blocked_sensitive": 1,
                    "blocked_size": 1,
                    "blocked_type": 0,
                }
            },
        }


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


def test_one_shot_renders_sandbox_review_counts(capsys):
    agent = _ReviewAgent()

    assert run_agent_once(agent, ["finish"]) == 0

    output = capsys.readouterr().out
    assert "State: pending_review" in output
    assert (
        "Changes: 3 candidate, 1 high-risk, 2 blocked, 4 generated (ignored)" in output
    )
    assert "Review: pony sandbox diff sandbox_" in output


def test_one_shot_keyboard_interrupt_finalizes_sandbox_and_returns_130():
    agent = _InterruptAgent()

    assert run_agent_once(agent, ["finish"]) == 130
    assert agent.finalized == 1


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="SIGTERM unavailable")
def test_one_shot_sigterm_becomes_interrupt_and_restores_handler():
    agent = _InterruptAgent(signum=signal.SIGTERM)
    previous = signal.getsignal(signal.SIGTERM)

    assert run_agent_once(agent, ["finish"]) == 128 + signal.SIGTERM
    assert agent.finalized == 1
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
    assert agent.finalized == 1


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
    assert agent.finalized == 1


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


def test_project_key_without_base_is_rejected(tmp_path, capsys):
    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=anthropic\n"
        "PONY_MODEL=claude-test\n"
        "PONY_API_KEY=stale-project-key\n",
        encoding="utf-8",
    )

    assert main(["--cwd", str(tmp_path), "--quiet", "run", "hello"]) == 3

    output = capsys.readouterr()
    assert output.err.splitlines()[0] == "api_base_not_configured"


def test_init_invalid_base_does_not_echo_input_value(tmp_path, monkeypatch, capsys):
    answers = iter(("", f"https://user:{CANARY}@example.com/v1"))
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    assert main(["--cwd", str(tmp_path), "init"]) == 3

    captured = capsys.readouterr()
    assert "api_base_credentials" in captured.err
    assert CANARY not in captured.out + captured.err
