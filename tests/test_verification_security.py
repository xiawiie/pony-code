import json
import sys
from unittest.mock import Mock

import pytest

import pico.agent.verification as verification
from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient


ACCEPTED_PREFIXES = (
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python3", "-m", "pytest"),
    ("ruff", "check"),
    ("python", "-m", "ruff", "check"),
    ("python3", "-m", "ruff", "check"),
    ("uv", "run", "pytest"),
    ("uv", "run", "ruff", "check"),
    ("uv", "run", "python", "-m", "pytest"),
    ("uv", "run", "python3", "-m", "pytest"),
    ("mypy",),
    ("pyright",),
    ("npm", "test"),
    ("pnpm", "test"),
    ("yarn", "test"),
    ("cargo", "test"),
    ("go", "test"),
)


@pytest.mark.parametrize("prefix", ACCEPTED_PREFIXES)
def test_verification_argv_accepts_only_pinned_prefixes(prefix):
    assert verification.is_verification_argv(prefix)
    assert verification.is_verification_argv((*prefix, "-q", "tests/test_unit.py"))


@pytest.mark.parametrize(
    "argv",
    (
        ("/usr/bin/pytest", "-q"),
        ("python", "-c", "import pytest"),
        ("uv", "pip", "install", "pytest"),
        ("tool", "run", "pytest"),
        ("uv", "run", "/usr/bin/python", "-m", "pytest"),
        ("sh", "-c", "pytest"),
        ("pytest", "||", "true"),
        ("pytest", "x||true"),
        ("pytest", "|", "tee", "out"),
        ("pytest", ">", "out"),
        ("pytest", "2>out"),
        ("pytest", "tests/.env"),
        ("pytest", "--config=.env"),
        ("pytest", "-c.env"),
        ("pytest", ".env::test_secret"),
        ("go", "test", "-exec=/bin/true", "./..."),
        ("go", "test", "-exec", "/bin/true", "./..."),
        ("npm", "test", "--script-shell=/bin/true"),
        ("npm", "test", "--node-options=--require=plugin.js"),
        ("pytest", "--python-executable", "/bin/true"),
        ("pytest", "-o", "cache_dir=.pico/checkpoints"),
        ("pytest", "-o", "python_files=.env"),
        ("pytest", "-o", "plugin_runner=/bin/true"),
        ("pytest", "-o", "plugin.wrapper=/bin/true"),
        ("pytest", "tests/private.key"),
        ("pytest", "bad\x00operand"),
        ("pytest", "bad\x1foperand"),
        ("pytest", "bad\toperand"),
        ("pytest", "bad\noperand"),
        ("pytest", "bad\u202eoperand"),
    ),
)
def test_verification_argv_rejects_wrappers_shell_tokens_and_sensitive_operands(argv):
    assert not verification.is_verification_argv(argv)


@pytest.mark.parametrize(
    "key",
    (
        "exec",
        "executable",
        "shell",
        "runner",
        "wrapper",
        "command",
        "cmd",
        "pythonpath",
        "program",
        "node-options",
    ),
)
def test_verification_argv_rejects_execution_control_config_keys(key):
    assert not verification.is_verification_argv(
        ("pytest", "-o", f"plugin_{key}=tests")
    )


@pytest.mark.parametrize(
    "argv",
    (
        ("pytest", "-o", "addopts=--override-ini=pythonpath=/tmp"),
        ("pytest", "--override-ini", "addopts=-o pythonpath=/tmp"),
        ("pytest", "-o", "addopts=--python-executable=/bin/true"),
        ("pytest", "--override-ini", "addopts=plugin_runner=/bin/true"),
        ("pytest", "-oaddopts=--script-shell=/bin/true"),
        ("pytest", "--override-ini=addopts=-q"),
    ),
)
def test_verification_argv_rejects_addopts_config_overrides(argv):
    assert not verification.is_verification_argv(argv)


def test_verification_argv_keeps_ordinary_relative_test_node_paths():
    assert verification.is_verification_argv(
        ("pytest", "tests/test_shell.py::test_exec")
    )


@pytest.mark.parametrize(
    "argv",
    (
        ("pytest", "-ktest_exec"),
        ("pytest", "-k", "test_exec"),
        ("pytest", "tests/test_\ue000.py"),
    ),
)
def test_verification_argv_accepts_pytest_short_values_and_private_use_paths(argv):
    assert verification.is_verification_argv(argv)


@pytest.mark.parametrize("prefix", ACCEPTED_PREFIXES)
def test_verification_evidence_requires_completed_structured_argv_execution(prefix):
    facts = {
        "argv": prefix,
        "risk_class": "external_effect",
        "runner_executed": True,
        "execution_mode": "argv",
        "exit_code": 0,
        "stdout": "passed",
        "stderr": "",
    }

    assert verification.verification_evidence_for_execution(
        **{**facts, "runner_executed": False}
    ) is None
    assert verification.verification_evidence_for_execution(
        **{**facts, "execution_mode": "shell"}
    ) is None
    assert verification.verification_evidence_for_execution(
        **{**facts, "exit_code": None}
    ) is None


def test_verification_evidence_rejects_aggregate_argv_over_field_bound():
    assert verification.verification_evidence_for_execution(
        argv=("pytest", "a" * 600, "b" * 600),
        risk_class="external_effect",
        runner_executed=True,
        execution_mode="argv",
        exit_code=0,
        stdout="passed",
        stderr="",
    ) is None


def _build_agent(root, command, *, approval_policy="ask", read_only=False):
    root.mkdir(parents=True)
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    call = {
        "name": "run_shell",
        "args": {"command": command, "timeout": 5},
    }
    agent = Pico(
        model_client=FakeModelClient([call, "done"]),
        workspace=WorkspaceContext.build(
            root,
            executables={
                "pytest": sys.executable,
                "python": sys.executable,
                "sh": sys.executable,
            },
        ),
        session_store=SessionStore(root / ".pico" / "sessions"),
        approval_policy=approval_policy,
        read_only=read_only,
    )
    return agent


@pytest.mark.parametrize(
    "command",
    (
        "pytest || true",
        "pytest | tee out",
        "pytest > out",
        "sh -c pytest",
    ),
)
def test_composite_or_wrapped_commands_create_no_verification_records(
    tmp_path,
    command,
):
    agent = _build_agent(tmp_path / command.split()[0], command)
    agent.approve = Mock(return_value=True)
    agent.tools["run_shell"]["run"] = Mock(
        return_value={"stdout": "passed", "stderr": "", "exit_code": 0}
    )

    assert agent.ask("run it") == "done"

    assert all(
        not record["verification_evidence"]
        for record in agent.checkpoint_store.list_checkpoint_records()
    )


@pytest.mark.parametrize(
    ("approval_policy", "read_only", "command"),
    (
        ("ask", False, "pytest -q"),
        ("ask", True, "pytest -q"),
        ("auto", False, "sh -c pytest"),
    ),
)
def test_blocked_shell_paths_create_no_verification_records(
    tmp_path,
    approval_policy,
    read_only,
    command,
):
    agent = _build_agent(
        tmp_path / f"{approval_policy}-{read_only}",
        command,
        approval_policy=approval_policy,
        read_only=read_only,
    )
    agent.approve = Mock(return_value=False)
    runner = Mock(
        return_value={"stdout": "passed", "stderr": "", "exit_code": 0}
    )
    agent.tools["run_shell"]["run"] = runner

    assert agent.ask("run it") == "done"

    runner.assert_not_called()
    assert agent.checkpoint_store.list_checkpoint_records() == []


def test_real_tool_executor_to_agent_loop_evidence_is_structured_redacted_and_bounded(
    tmp_path,
    monkeypatch,
):
    secret = "ghp_" + "B" * 32
    monkeypatch.setenv("PICO_TEST_SECRET", secret)
    agent = _build_agent(tmp_path / "structured", "python -m pytest -q")
    agent.approve = Mock(return_value=True)
    agent.tools["run_shell"]["run"] = Mock(
        return_value={
            "stdout": "all tests passed " + "x" * 1200 + secret,
            "stderr": "failure detail " + "y" * 1200 + secret,
            "exit_code": 7,
        }
    )

    assert agent.ask("run verification") == "done"

    checkpoint = agent.checkpoint_store.load_checkpoint_record(
        agent.current_task_state.recovery_checkpoint_id
    )
    assert len(checkpoint["verification_evidence"]) == 1
    evidence = checkpoint["verification_evidence"][0]
    assert evidence["status"] == "failed"
    assert evidence["exit_code"] == 7
    assert secret not in json.dumps(evidence)
    assert len(evidence["command"]) <= 1000
    assert len(evidence["stdout_tail"]) <= 1000
    assert len(evidence["stderr_tail"]) <= 1000
