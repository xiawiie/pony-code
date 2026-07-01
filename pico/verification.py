"""在 checkpoint 上挂命令级别的验证证据。

一次 `python -m pytest -q` 的成功或失败，是判断一个 turn 是否值得保留的关键
证据。这里把命令、退出码、stdout/stderr 的尾部（避免过大）打包成一条
Verification Record，方便 checkpoint 与 trace 双向引用。
"""

from pico.recovery_models import (
    VERIFICATION_RECORD_SCHEMA_VERSION,
    new_id,
    utc_now,
)


_MAX_TAIL_CHARS = 1000


def _tail(text):
    if not text:
        return ""
    text = str(text)
    if len(text) <= _MAX_TAIL_CHARS:
        return text
    return text[-_MAX_TAIL_CHARS:]


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
