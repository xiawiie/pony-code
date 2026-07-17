import os
import signal
import time
from types import SimpleNamespace

from pony.tools.subprocess import (
    build_trusted_executables,
    run_process_group,
)
from pony.tools.shell import ApprovedShellExecution, _tool_run_shell


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _read_pid(path):
    return int(path.read_text(encoding="utf-8")) if path.exists() else None


def test_sandbox_runner_reaps_timed_out_process_group(tmp_path):
    python = build_trusted_executables(tmp_path, names=("python3",))["python3"]
    result = run_process_group(
        [python, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        timeout=0.01,
    )
    assert result.timed_out is True
    assert result.exit_code != 0


def test_production_hardened_timeout_reaps_child_and_grandchild(tmp_path):
    child_pid_path = tmp_path / "child.pid"
    grandchild_pid_path = tmp_path / "grandchild.pid"
    script = tmp_path / "process_tree.py"
    script.write_text(
        """
import signal
import subprocess
import sys
import time
from pathlib import Path

child_code = r'''\
import signal
import subprocess
import sys
import time
from pathlib import Path

signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
])
Path(sys.argv[1]).write_text(str(grandchild.pid), encoding="utf-8")
time.sleep(30)
'''

signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([
    sys.executable,
    "-c",
    child_code,
    sys.argv[2],
])
Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
deadline = time.monotonic() + 5
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
time.sleep(30)
""",
        encoding="utf-8",
    )
    python = build_trusted_executables(tmp_path, names=("python3",))["python3"]

    try:
        result = _tool_run_shell(
            SimpleNamespace(
                root=tmp_path,
                shell_env=lambda: {"PATH": "/usr/bin:/bin"},
            ),
            ApprovedShellExecution(
                exact_command="",
                argv=(
                    "python3",
                    str(script),
                    str(child_pid_path),
                    str(grandchild_pid_path),
                ),
                execution_mode="argv",
                executable=python,
                timeout=1,
            ),
        )

        assert result["timed_out"] is True
        assert result["sandbox_outcome"] == "timeout"
        assert result["exit_code"] != 0
        pids = (_read_pid(child_pid_path), _read_pid(grandchild_pid_path))
        assert all(pids)
        deadline = time.monotonic() + 3
        while any(_pid_exists(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not any(_pid_exists(pid) for pid in pids)
    finally:
        for path in (child_pid_path, grandchild_pid_path):
            pid = _read_pid(path)
            if pid is not None and _pid_exists(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
