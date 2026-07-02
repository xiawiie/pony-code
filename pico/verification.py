"""在 checkpoint 上挂命令级别的验证证据。

一次 `python -m pytest -q` 的成功或失败，是判断一个 turn 是否值得保留的关键
证据。这里把命令、退出码、stdout/stderr 的尾部（避免过大）打包成一条
Verification Record，方便 checkpoint 与 trace 双向引用。
"""

from pathlib import Path
import shlex

from pico.recovery_models import (
    VERIFICATION_RECORD_SCHEMA_VERSION,
    new_id,
    utc_now,
)


_MAX_TAIL_CHARS = 1000
_VERIFICATION_COMMAND_MARKERS = (
    "pytest",
    "ruff",
    "mypy",
    "pyright",
    "npm test",
    "pnpm test",
    "yarn test",
    "cargo test",
    "go test",
)
_NON_VERIFICATION_HEADS = {"rg", "grep", "find", "cat", "echo", "printf", "sed", "awk"}
_SINGLE_TOOL_PREDECESSORS = {"run", "-m", "exec"}


def _tail(text):
    if not text:
        return ""
    text = str(text)
    if len(text) <= _MAX_TAIL_CHARS:
        return text
    return text[-_MAX_TAIL_CHARS:]


def is_verification_command(command):
    try:
        tokens = shlex.split(str(command or ""))
    except ValueError:
        tokens = str(command or "").split()
    normalized = [Path(token).name.lower() for token in tokens]
    if not normalized:
        return False
    if normalized[0] in _NON_VERIFICATION_HEADS:
        return False
    for marker in _VERIFICATION_COMMAND_MARKERS:
        marker_tokens = marker.split()
        if len(marker_tokens) == 1:
            for index, token in enumerate(normalized):
                if token != marker_tokens[0]:
                    continue
                if index == 0 or normalized[index - 1] in _SINGLE_TOOL_PREDECESSORS:
                    return True
            continue
        for index in range(0, len(normalized) - len(marker_tokens) + 1):
            if normalized[index:index + len(marker_tokens)] == marker_tokens:
                return True
    return False


def parse_run_shell_result(text):
    content = str(text or "")
    exit_code = 1
    stdout = ""
    stderr = ""
    lines = content.splitlines()
    if lines and lines[0].startswith("exit_code:"):
        try:
            exit_code = int(lines[0].split(":", 1)[1].strip())
        except ValueError:
            exit_code = 1
    stdout_marker = "\nstdout:\n"
    stderr_marker = "\nstderr:\n"
    if stdout_marker in content:
        after_stdout = content.split(stdout_marker, 1)[1]
        if stderr_marker in after_stdout:
            stdout, stderr = after_stdout.split(stderr_marker, 1)
        else:
            stdout = after_stdout
    return {
        "exit_code": exit_code,
        "stdout": "" if stdout.strip() == "(empty)" else stdout.strip(),
        "stderr": "" if stderr.strip() == "(empty)" else stderr.strip(),
    }


def new_verification_record(
    command,
    risk_class,
    exit_code,
    stdout,
    stderr,
    affected_checkpoint_id="",
    trace_event_id="",
):
    status = "passed" if int(exit_code) == 0 else "failed"
    return {
        "schema_version": VERIFICATION_RECORD_SCHEMA_VERSION,
        "verification_id": new_id("verify"),
        "created_at": utc_now(),
        "command": str(command),
        "risk_class": str(risk_class),
        "exit_code": int(exit_code),
        "status": status,
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
        "affected_checkpoint_id": str(affected_checkpoint_id or ""),
        "trace_event_id": str(trace_event_id or ""),
    }
