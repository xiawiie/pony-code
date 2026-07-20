import signal
import sys
import time

import pytest

from pony.tools.subprocess import ProcessOutputLimitExceeded, run_process_group
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
    command = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    )
    result = run_process_group(
        [sys.executable, "-c", command],
        cwd="/tmp",
        env={},
        timeout=0.1,
        term_grace=0.1,
    )

    assert result.timed_out is True
    assert result.returncode == -signal.SIGKILL


def test_process_group_output_limit_terminates_without_unbounded_capture(tmp_path):
    started = time.monotonic()
    child_marker = tmp_path / "child-survived"
    child = (
        "import pathlib,time; time.sleep(0.5); "
        f"pathlib.Path({str(child_marker)!r}).write_text('alive')"
    )
    parent = (
        "import os,subprocess\n"
        f"subprocess.Popen([{sys.executable!r}, '-c', {child!r}])\n"
        "os.write(1, b'x' * 8192)"
    )

    with pytest.raises(
        ProcessOutputLimitExceeded,
        match="^process_output_limit_exceeded$",
    ):
        run_process_group(
            [
                sys.executable,
                "-c",
                parent,
            ],
            cwd="/tmp",
            env={},
            timeout=10,
            term_grace=0.1,
            max_output_bytes=1024,
        )

    assert time.monotonic() - started < 2
    time.sleep(0.7)
    assert not child_marker.exists()
