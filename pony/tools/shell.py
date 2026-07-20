"""Frozen shell execution plans and the host shell runner."""

from dataclasses import dataclass
from pathlib import Path

from pony.tools.subprocess import run_hardened_command


DEFAULT_RUN_SHELL_TIMEOUT = 60

MAX_RUN_SHELL_TIMEOUT = 120


@dataclass(frozen=True)
class ApprovedShellExecution:
    exact_command: str
    argv: tuple[str, ...]
    execution_mode: str
    executable: str
    timeout: int


def _tool_run_shell(context, execution):
    if not isinstance(execution, ApprovedShellExecution):
        raise ValueError("run_shell requires an approved execution plan")
    if not Path(execution.executable).is_absolute():
        raise ValueError("trusted executable must be absolute")
    if execution.execution_mode == "argv":
        if not execution.argv:
            raise ValueError("approved argv must not be empty")
        result = run_hardened_command(
            execution.executable,
            args=execution.argv[1:],
            cwd=context.root,
            timeout=execution.timeout,
            env=context.shell_env(),
            return_timeout=True,
        )
    elif execution.execution_mode == "shell":
        result = run_hardened_command(
            execution.executable,
            command=execution.exact_command,
            shell=True,
            cwd=context.root,
            timeout=execution.timeout,
            env=context.shell_env(),
            return_timeout=True,
        )
    else:
        raise ValueError("unsupported approved execution mode")
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
        "timed_out": result.timed_out,
    }
