#!/usr/bin/env python3
"""Exercise Pony's production Windows Job Object process backend."""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback


def _stage(name):
    print(f"windows_process_stage={name}", file=sys.stderr, flush=True)


def _delayed_marker_code(marker):
    return (
        "import pathlib,time; time.sleep(0.8); "
        f"pathlib.Path({str(marker)!r}).write_bytes(b'alive')"
    )


def _tree_parent_code(pid_path, marker, *, flood=False):
    child = _delayed_marker_code(marker)
    output = "import os; os.write(1, b'x' * 8192)" if flood else "print('ready', flush=True)"
    return (
        "import pathlib,subprocess,sys,time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid), encoding='ascii')\n"
        f"{output}\n"
        "time.sleep(60)\n"
    )


def probe():
    root_path = Path(__file__).resolve().parents[2]
    if str(root_path) not in sys.path:
        sys.path.insert(0, str(root_path))
    from pony.tools.subprocess import ProcessOutputLimitExceeded, run_process_group

    with tempfile.TemporaryDirectory(prefix="pony-windows-process-") as temporary:
        root = Path(temporary)
        env = dict(os.environ)

        _stage("capture")
        result = run_process_group(
            [
                sys.executable,
                "-c",
                "import os,sys; print(os.getcwd()); sys.stderr.write('stderr\\n')",
            ],
            cwd=root,
            env=env,
            timeout=5,
        )
        reported_cwd = Path(result.stdout.strip())
        if result.returncode != 0 or not reported_cwd.samefile(root):
            raise RuntimeError(
                "Windows process capture result mismatch: "
                f"returncode={result.returncode}, "
                f"stdout={result.stdout!r}, stderr={result.stderr!r}"
            )
        if result.stderr != "stderr\n" or result.timed_out:
            raise RuntimeError("Windows stderr capture result mismatch")

        _stage("parent_exit")
        parent_exit_marker = root / "parent-exit-survived"
        parent_exit_child = _delayed_marker_code(parent_exit_marker)
        result = run_process_group(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess,sys\n"
                    f"subprocess.Popen([sys.executable, '-c', {parent_exit_child!r}])\n"
                    "print('parent-exited', flush=True)\n"
                ),
            ],
            cwd=root,
            env=env,
            timeout=5,
        )
        if result.returncode != 0 or result.timed_out:
            raise RuntimeError("Windows parent exit did not complete normally")
        if result.stdout != "parent-exited\n":
            raise RuntimeError("Windows parent-exit capture result mismatch")
        time.sleep(1)
        if parent_exit_marker.exists():
            raise RuntimeError("Windows parent exit left a descendant alive")

        _stage("timeout")
        timeout_pid = root / "timeout-pid"
        timeout_marker = root / "timeout-survived"
        result = run_process_group(
            [
                sys.executable,
                "-c",
                _tree_parent_code(timeout_pid, timeout_marker),
            ],
            cwd=root,
            env=env,
            timeout=0.3,
        )
        if not result.timed_out or "ready" not in result.stdout or not timeout_pid.exists():
            raise RuntimeError("Windows process timeout did not start and terminate the tree")
        time.sleep(1)
        if timeout_marker.exists():
            raise RuntimeError("Windows process timeout left a descendant alive")

        _stage("output_limit")
        output_pid = root / "output-pid"
        output_marker = root / "output-survived"
        try:
            run_process_group(
                [
                    sys.executable,
                    "-c",
                    _tree_parent_code(output_pid, output_marker, flood=True),
                ],
                cwd=root,
                env=env,
                timeout=5,
                max_output_bytes=1024,
            )
        except ProcessOutputLimitExceeded:
            pass
        else:
            raise RuntimeError("Windows process output limit was ignored")
        if not output_pid.exists():
            raise RuntimeError("Windows process output-limit tree did not start")
        time.sleep(1)
        if output_marker.exists():
            raise RuntimeError("Windows process output limit left a descendant alive")

    return {
        "schema_version": 1,
        "bounded_capture": True,
        "create_suspended_before_job_assignment": True,
        "parent_exit_kills_descendants": True,
        "timeout_kills_descendants": True,
        "output_limit_kills_descendants": True,
    }


def main():
    if os.name != "nt":
        print("windows_process_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        report = probe()
    except BaseException as exc:
        traceback.print_exc()
        print(f"windows_process_probe_failed: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
