import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pony.tools.subprocess import run_process_group
from pony.tools.shell import ApprovedShellExecution


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
