import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pony.tools.subprocess import run_process_group
from pony.tools.shell import ApprovedShellExecution, sandbox_privilege_denial


def test_approved_shell_execution_is_immutable_and_complete(tmp_path):
    execution = ApprovedShellExecution(
        argv=("printf", "%s", "ok"),
        exact_command="printf %s ok",
        execution_mode="argv",
        executable="/usr/bin/printf",
        timeout=5,
    )
    assert execution.argv == ("printf", "%s", "ok")
    with pytest.raises(Exception):
        execution.timeout = 2


def test_sandbox_privilege_deny_uses_executable_identity_not_prefix():
    execution = ApprovedShellExecution(
        argv=("sudo", "true"),
        exact_command="sudo true",
        execution_mode="argv",
        executable="/usr/bin/sudo",
        timeout=5,
    )
    assert sandbox_privilege_denial(execution, sandbox_mode=False) is None
    assert (
        sandbox_privilege_denial(execution, sandbox_mode=True)
        == "sandbox_privilege_denied"
    )

    harmless = ApprovedShellExecution(
        argv=("sudo-helper",),
        exact_command="sudo-helper",
        execution_mode="argv",
        executable="/tmp/sudo-helper",
        timeout=5,
    )
    assert sandbox_privilege_denial(harmless, sandbox_mode=True) is None


@pytest.mark.parametrize(
    "argv,command",
    [
        (("sh", "-c", "echo x; open /tmp"), "echo x; open /tmp"),
        (("sh", "-c", "open /tmp"), 'sh -c "open /tmp"'),
        (("env", "open", "/tmp"), "env open /tmp"),
    ],
)
def test_sandbox_privilege_deny_recurses_through_shell_and_wrappers(argv, command):
    execution = ApprovedShellExecution(
        argv=argv,
        exact_command=command,
        execution_mode="complex_shell",
        executable="/bin/sh",
        timeout=5,
    )
    assert (
        sandbox_privilege_denial(execution, sandbox_mode=True)
        == "sandbox_privilege_denied"
    )


def test_process_group_timeout_terms_then_kills_and_waits():
    process = SimpleNamespace(returncode=None)
    calls = 0

    def communicate(timeout=None):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise subprocess.TimeoutExpired("x", timeout)
        process.returncode = -9
        return "", ""

    process.communicate = communicate
    process.pid = 4321
    with (
        patch("pony.tools.subprocess.subprocess.Popen", return_value=process) as popen,
        patch("pony.tools.subprocess.os.killpg") as killpg,
    ):
        result = run_process_group(["x"], cwd="/tmp", env={}, timeout=1, term_grace=2)
    assert popen.call_args.kwargs["start_new_session"] is True
    assert [call.args[1] for call in killpg.call_args_list] == [15, 9]
    assert result.timed_out is True
