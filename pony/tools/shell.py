"""Frozen shell execution plans and the host shell runner."""

from dataclasses import dataclass
from pathlib import Path
import re
import shlex

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


_SANDBOX_PRIVILEGED_EXECUTABLES = frozenset(
    {"sudo", "doas", "pkexec", "open", "osascript", "launchctl"}
)


def sandbox_privilege_denial(
    execution,
    *,
    sandbox_mode,
    allow_git_metadata_writes=False,
):
    if not sandbox_mode:
        return None
    executable_name = Path(str(execution.executable)).name.casefold()
    argv_name = (
        Path(str(execution.argv[0])).name.casefold()
        if execution.argv
        else executable_name
    )
    command = str(getattr(execution, "exact_command", "") or "")
    tokens = []
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "sandbox_privilege_denied"
    # Inspect shell segments and wrapper payloads (sh -c, env, command) so an
    # alias cannot turn a broker call into an apparently harmless argv.
    normalized = re.sub(r"(?:&&|\|\||[;|])", " ", command)
    try:
        tokens.extend(shlex.split(normalized, posix=True))
    except ValueError:
        return "sandbox_privilege_denied"
    expanded = [*tokens, *(str(value) for value in execution.argv)]
    for token in tuple(expanded):
        if any(character.isspace() for character in token):
            try:
                expanded.extend(shlex.split(token, posix=True))
            except ValueError:
                return "sandbox_privilege_denied"
    names = {executable_name, argv_name}
    names.update(Path(token).name.casefold() for token in expanded)
    if any(name in _SANDBOX_PRIVILEGED_EXECUTABLES for name in names):
        return "sandbox_privilege_denied"
    if executable_name == "git" and not allow_git_metadata_writes:
        git_subcommands = {
            "add",
            "commit",
            "reset",
            "checkout",
            "merge",
            "rebase",
            "update-index",
        }
        if any(
            str(argument) in git_subcommands
            for argument in (*execution.argv[1:], *tokens)
        ):
            return "sandbox_git_metadata_write_denied"
    return None


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
        "sandbox_outcome": "timeout" if result.timed_out else "not_applicable",
    }
