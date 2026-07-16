import os
from unittest.mock import Mock

import pytest

from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient
from pico.recovery.policy import assess_command


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    executables = kwargs.pop("executables", None)
    workspace = WorkspaceContext.build(
        tmp_path,
        executables=executables,
    )
    store = SessionStore(tmp_path / ".pico" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


ALLOW_ARGV = (
    "pwd",
    "ls",
    "ls -1 README.md",
    "stat README.md",
    "file --brief README.md",
    "wc -l README.md",
    "git status --short",
    "git rev-parse HEAD",
    "git branch --show-current",
    "git worktree list",
    "git ls-files",
)

ASK_ARGV = (
    "unknown-binary --version",
    "python -m pytest -q",
    "bash -c 'pwd && pwd'",
    "sudo true",
    "systemctl status sshd",
    "npm test",
    "curl https://example.invalid",
    "./ls",
    "/bin/ls",
    "date -s 2030-01-01",
    "rg --pre cat token .",
    "git diff --ext-diff",
    "git log -1",
    "env pwd",
    "xargs printf",
)

ASK_SHELL = (
    "pwd | wc -l",
    "pwd && pwd",
    "pwd || true",
    "pwd; pwd",
    "printf x > out.txt",
    "if true; then pwd; fi",
    "$(pwd)",
    "`pwd`",
    "cat <<EOF\nbody\nEOF",
    "find . -exec printf x ;",
)

REJECT = (
    "cat .env",
    "printf x > .env",
    "ls .ssh",
    "cat .pico/sessions/session.json",
)


@pytest.mark.parametrize("command", ALLOW_ARGV)
def test_exact_auto_grammar_is_allow_argv(tmp_path, command):
    (tmp_path / "README.md").write_text("safe\n", encoding="utf-8")
    assessment = assess_command(command, tmp_path)
    assert assessment["decision"] == "allow"
    assert assessment["execution_mode"] == "argv"
    assert assessment["argv"]


@pytest.mark.parametrize("command", ASK_ARGV)
def test_simple_risky_or_unknown_commands_are_ask_argv(tmp_path, command):
    assessment = assess_command(command, tmp_path)
    assert assessment["decision"] == "ask"
    assert assessment["execution_mode"] == "argv"
    assert assessment["argv"]


@pytest.mark.parametrize("command", ASK_SHELL)
def test_shell_grammar_is_ask_shell(tmp_path, command):
    assessment = assess_command(command, tmp_path)
    assert assessment["decision"] == "ask"
    assert assessment["execution_mode"] == "shell"


@pytest.mark.parametrize("command", REJECT)
def test_literal_sensitive_targets_are_hard_reject(tmp_path, command):
    assessment = assess_command(command, tmp_path)
    assert assessment["decision"] == "reject"


@pytest.mark.parametrize("command", ASK_ARGV + ASK_SHELL + REJECT)
def test_auto_mode_never_calls_runner_for_non_allow_command(tmp_path, command):
    agent = build_agent(tmp_path, [], approval_policy="auto")
    runner = Mock(return_value="must not run")
    agent.tools["run_shell"]["run"] = runner

    result = agent.execute_tool(
        "run_shell",
        {"command": command, "timeout": 20},
    )

    assert result.metadata["tool_status"] == "rejected"
    approval = result.metadata["command_approval"]
    assert approval["runner_executed"] is False
    assert approval["outcome"] in {"blocked", "denied"}
    assert approval["execution_mode"] in {"argv", "shell"}
    runner.assert_not_called()


def test_ask_mode_approved_simple_command_stays_argv(tmp_path, monkeypatch):
    agent = build_agent(
        tmp_path,
        [],
        approval_policy="ask",
        executables={"python": "/frozen/python"},
    )
    monkeypatch.setattr(agent, "approve", lambda name, args: True)
    runner = Mock(
        return_value={"stdout": "passed\n", "stderr": "", "exit_code": 0}
    )
    agent.tools["run_shell"]["run"] = runner

    result = agent.execute_tool(
        "run_shell",
        {"command": "python -m pytest -q", "timeout": 20},
    )

    approval = result.metadata["command_approval"]
    assert approval["outcome"] == "approved"
    assert approval["runner_executed"] is True
    assert approval["execution_mode"] == "argv"
    runner.assert_called_once()


def test_read_only_and_never_modes_do_not_prompt_or_run(tmp_path, monkeypatch):
    configurations = (
        {"approval_policy": "never"},
        {"approval_policy": "ask", "read_only": True},
    )
    for kwargs in configurations:
        agent = build_agent(tmp_path, [], **kwargs)
        approve = Mock(return_value=True)
        runner = Mock(return_value="must not run")
        monkeypatch.setattr(agent, "approve", approve)
        agent.tools["run_shell"]["run"] = runner

        result = agent.execute_tool(
            "run_shell",
            {"command": "python -m pytest -q", "timeout": 20},
        )

        assert result.metadata["tool_status"] == "rejected"
        approve.assert_not_called()
        runner.assert_not_called()


def test_workspace_binary_and_relative_path_never_win_trust(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_git.chmod(0o755)
    unsafe_path = ".:" + str(fake_bin) + ":/usr/bin:/bin"

    from pico.tools.subprocess import build_trusted_executables

    trusted = build_trusted_executables(
        tmp_path,
        env={"PATH": unsafe_path},
        names=("git",),
    )

    assert trusted.get("git") != str(fake_git)


def test_hardened_git_and_rg_ignore_executable_repo_config(tmp_path, monkeypatch):
    from pico.tools.subprocess import (
        build_trusted_executables,
        run_hardened_git,
        run_hardened_rg,
    )

    calls = []

    def fake_run(argv, **kwargs):
        calls.append((tuple(argv), dict(kwargs)))
        return type(
            "Completed",
            (),
            {"returncode": 0, "stdout": b"", "stderr": b""},
        )()

    monkeypatch.setattr("pico.tools.subprocess.subprocess.run", fake_run)
    executables = build_trusted_executables(
        tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "RIPGREP_CONFIG_PATH": str(tmp_path / "rg.conf"),
            "GIT_CONFIG_COUNT": "1",
        },
        names=("git", "rg"),
    )
    if "git" in executables:
        run_hardened_git(
            executables["git"],
            ["status", "--short"],
            cwd=tmp_path,
        )
    if "rg" in executables:
        run_hardened_rg(
            executables["rg"],
            ["token", "."],
            cwd=tmp_path,
        )

    for argv, kwargs in calls:
        joined = " ".join(argv)
        env = kwargs["env"]
        assert "core.fsmonitor=false" in joined or argv[0].endswith("rg")
        if argv[0].endswith("rg"):
            assert env["RIPGREP_CONFIG_PATH"] == os.devnull
        else:
            assert "RIPGREP_CONFIG_PATH" not in env
        assert "GIT_CONFIG_COUNT" not in env
        assert all(
            key
            in {
                "GIT_ALLOW_PROTOCOL",
                "GIT_CONFIG_NOSYSTEM",
                "GIT_CONFIG_GLOBAL",
                "GIT_TERMINAL_PROMPT",
            }
            for key in env
            if key.startswith("GIT_")
        )
