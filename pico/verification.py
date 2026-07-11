"""Admit verification evidence from completed, exact argv executions."""

import shlex
import unicodedata

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
_OPERAND_DELIMITERS = ("=", ":", ",")
_EXECUTION_CONTROL_KEY_MARKERS = (
    "exec",
    "executable",
    "shell",
    "runner",
    "wrapper",
    "command",
    "cmd",
    "pythonpath",
    "program",
    "nodeoptions",
)
_CONFIG_VALUE_OPTIONS = frozenset(("o", "overrideini", "config"))


def _tail(text):
    if not text:
        return ""
    text = str(text)
    if len(text) <= _MAX_TAIL_CHARS:
        return text
    return text[-_MAX_TAIL_CHARS:]


def _strip_operand_syntax(value):
    return str(value).strip().strip("\"'").strip()


def _operand_fragments(token):
    pending = [token]
    seen = set()
    while pending:
        candidate = _strip_operand_syntax(pending.pop())
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        yield candidate
        for delimiter in _OPERAND_DELIMITERS:
            if delimiter in candidate:
                pending.extend(candidate.split(delimiter))
        if candidate.startswith("-"):
            pending.append(candidate.lstrip("-"))
            if len(candidate) > 2:
                pending.append(candidate[2:])
            dot_index = candidate.find(".", 2)
            if dot_index >= 0:
                pending.append(candidate[dot_index:])


def _sensitive_operand(token):
    return any(
        securitylib.is_sensitive_path(candidate)
        for candidate in _operand_fragments(token)
    )


def _execution_control_key(value):
    compact = "".join(
        char
        for char in _strip_operand_syntax(value).casefold()
        if char.isalnum()
    )
    return any(marker in compact for marker in _EXECUTION_CONTROL_KEY_MARKERS)


def _option_parts(token):
    candidate = _strip_operand_syntax(token)
    if not candidate.startswith("-"):
        return "", None
    body = candidate.lstrip("-")
    separator_indexes = [
        body.index(separator)
        for separator in ("=", ":")
        if separator in body
    ]
    if not separator_indexes:
        return body, None
    index = min(separator_indexes)
    return body[:index], body[index + 1:]


def _config_key(value):
    candidate = _strip_operand_syntax(value)
    indexes = [
        candidate.index(separator)
        for separator in ("=", ":")
        if separator in candidate
    ]
    return candidate if not indexes else candidate[:min(indexes)]


def _has_execution_control_tail(tokens):
    config_value_expected = False
    for token in tokens:
        if config_value_expected:
            if _execution_control_key(_config_key(token)):
                return True
            config_value_expected = False
        option_key, inline_value = _option_parts(token)
        if option_key and _execution_control_key(option_key):
            return True
        compact_option = "".join(
            char for char in option_key.casefold() if char.isalnum()
        )
        if compact_option in _CONFIG_VALUE_OPTIONS:
            if inline_value is None:
                config_value_expected = True
            elif _execution_control_key(_config_key(inline_value)):
                return True
    return False


def is_verification_argv(argv):
    if isinstance(argv, (str, bytes)):
        return False
    try:
        tokens = tuple(argv)
    except TypeError:
        return False
    if not tokens or any(type(token) is not str or not token for token in tokens):
        return False
    if any(
        any(
            char in _SHELL_TOKEN_CHARS
            or unicodedata.category(char).startswith("C")
            for char in token
        )
        for token in tokens
    ):
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
    tail = tokens[len(prefix):]
    return not _has_execution_control_tail(tail) and not any(
        _sensitive_operand(token) for token in tail
    )


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
