"""Admit verification evidence from completed, exact argv executions."""

import shlex

from pico import security as securitylib
from pico.recovery_models import (
    VERIFICATION_RECORD_SCHEMA_VERSION,
    new_id,
    utc_now,
)


_MAX_TAIL_CHARS = 1000
_MAX_ARGV_TOKENS = 128
_VERIFICATION_PREFIXES = (
    ("uv", "run", "python3", "-m", "pytest"),
    ("uv", "run", "python", "-m", "pytest"),
    ("python3", "-m", "ruff", "check"),
    ("python", "-m", "ruff", "check"),
    ("uv", "run", "ruff", "check"),
    ("python3", "-m", "pytest"),
    ("python", "-m", "pytest"),
    ("uv", "run", "pytest"),
    ("ruff", "check"),
    ("npm", "test"),
    ("pnpm", "test"),
    ("yarn", "test"),
    ("cargo", "test"),
    ("go", "test"),
    ("pytest",),
    ("mypy",),
    ("pyright",),
)
_SHELL_TOKEN_CHARS = frozenset("\0\r\n|&;<>()`")


def _tail(text):
    if not text:
        return ""
    text = str(text)
    if len(text) <= _MAX_TAIL_CHARS:
        return text
    return text[-_MAX_TAIL_CHARS:]


def _sensitive_operand(token):
    candidates = [token]
    if ":" in token:
        candidates.extend(part for part in token.split(":") if part)
    if token.startswith("-") and len(token) > 2:
        candidates.append(token[2:])
    if token.startswith("-") and "=" in token:
        candidates.append(token.split("=", 1)[1])
    dot_index = token.find(".", 2)
    if token.startswith("-") and dot_index >= 0:
        candidates.append(token[dot_index:])
    return any(securitylib.is_sensitive_path(candidate) for candidate in candidates)


def is_verification_argv(argv):
    if isinstance(argv, (str, bytes)):
        return False
    try:
        tokens = tuple(argv)
    except TypeError:
        return False
    if not tokens or any(type(token) is not str or not token for token in tokens):
        return False
    if any(any(char in _SHELL_TOKEN_CHARS for char in token) for token in tokens):
        return False
    prefix = next(
        (
            candidate
            for candidate in _VERIFICATION_PREFIXES
            if tokens[:len(candidate)] == candidate
        ),
        None,
    )
    if prefix is None:
        return False
    return not any(_sensitive_operand(token) for token in tokens[len(prefix):])


def _redacted(redact_text, value):
    text = str(value)
    return str(redact_text(text)) if callable(redact_text) else text


def _head(text):
    return str(text)[:_MAX_TAIL_CHARS]


def verification_evidence_for_execution(
    *,
    argv,
    risk_class,
    runner_executed,
    execution_mode,
    exit_code,
    stdout,
    stderr,
    redact_text=None,
):
    if (
        runner_executed is not True
        or execution_mode != "argv"
        or type(exit_code) is not int
        or type(risk_class) is not str
        or type(stdout) is not str
        or type(stderr) is not str
        or not is_verification_argv(argv)
    ):
        return None
    tokens = tuple(argv)
    if len(tokens) > _MAX_ARGV_TOKENS or any(
        len(token) > _MAX_TAIL_CHARS for token in tokens
    ) or len(shlex.join(tokens)) > _MAX_TAIL_CHARS:
        return None
    safe_tokens = tuple(_redacted(redact_text, token) for token in tokens)
    if safe_tokens != tokens:
        return None
    return {
        "argv": list(safe_tokens),
        "runner_executed": True,
        "execution_mode": "argv",
        "exit_code": exit_code,
        "risk_class": _head(_redacted(redact_text, risk_class)),
        "stdout": _tail(_redacted(redact_text, stdout)),
        "stderr": _tail(_redacted(redact_text, stderr)),
    }


def new_verification_record(
    *,
    argv,
    risk_class,
    runner_executed,
    execution_mode,
    exit_code,
    stdout,
    stderr,
    affected_checkpoint_id="",
    trace_event_id="",
    redact_text=None,
):
    evidence = verification_evidence_for_execution(
        argv=argv,
        risk_class=risk_class,
        runner_executed=runner_executed,
        execution_mode=execution_mode,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        redact_text=redact_text,
    )
    if evidence is None:
        return None
    return {
        "schema_version": VERIFICATION_RECORD_SCHEMA_VERSION,
        "verification_id": new_id("verify"),
        "created_at": utc_now(),
        "argv": list(evidence["argv"]),
        "runner_executed": True,
        "execution_mode": "argv",
        "command": _head(shlex.join(evidence["argv"])),
        "risk_class": evidence["risk_class"],
        "exit_code": evidence["exit_code"],
        "status": "passed" if evidence["exit_code"] == 0 else "failed",
        "stdout_tail": evidence["stdout"],
        "stderr_tail": evidence["stderr"],
        "affected_checkpoint_id": _head(
            _redacted(redact_text, affected_checkpoint_id or "")
        ),
        "trace_event_id": _head(_redacted(redact_text, trace_event_id or "")),
    }
