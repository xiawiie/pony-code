"""Request-scoped safe projection for transient streaming text."""

import re
import time

from pony.security.redaction import MIN_SECRET_SUBSTRING_REDACTION_LENGTH
from pony.security.text import (
    contains_isolated_surrogate,
    normalize_surrogate_pairs,
    sanitize_terminal_security_line,
)


MAX_PREVIEW_LINE_BYTES = 64 * 1024
_PRIVATE_KEY_BEGIN = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----"
)


class SafeTextPreview:
    """Release only complete, redacted lines to a non-durable sink."""

    def __init__(
        self,
        *,
        redact_text,
        secret_values=(),
        on_stream_committed=None,
        on_safe_preview=None,
        clock=time.monotonic,
    ):
        self._redact_text = redact_text
        self._on_stream_committed = on_stream_committed
        self._on_safe_preview = on_safe_preview
        self._clock = clock
        self._started_at = clock()
        self._buffer = ""
        self._raw_snapshot = ""
        self._safe_snapshot = ""
        raw_secret_values = tuple(str(value) for value in secret_values if value)
        self._secret_values = tuple(
            normalize_surrogate_pairs(value) for value in raw_secret_values
        )
        self._disabled = any(
            "\r" in value
            or "\n" in value
            or len(value) < MIN_SECRET_SUBSTRING_REDACTION_LENGTH
            or sanitize_terminal_security_line(value) != value
            for value in raw_secret_values
        )
        self._committed = False
        self._preview_emitted = False
        self._first_preview_ms = None

    def metadata(self):
        return {
            "requested": True,
            "committed": self._committed,
            "preview_emitted": self._preview_emitted,
            "first_preview_ms": self._first_preview_ms,
        }

    def stream_committed(self):
        if self._committed:
            return
        self._committed = True
        callback = self._on_stream_committed
        if not callable(callback):
            return
        try:
            callback()
        except Exception:
            pass

    def text_delta(self, delta):
        if self._disabled or not self._committed or not isinstance(delta, str):
            return
        try:
            self._buffer += delta
            self._release_complete_lines()
            pending_bytes = len(self._buffer.encode("utf-8"))
        except Exception:
            pending_bytes = MAX_PREVIEW_LINE_BYTES + 1
        if pending_bytes > MAX_PREVIEW_LINE_BYTES:
            self._disabled = True
            self._buffer = ""

    def _release_complete_lines(self):
        while not self._disabled:
            boundary = self._line_boundary()
            if boundary is None:
                return
            line_end, delimiter_end = boundary
            line = self._buffer[:line_end]
            self._buffer = self._buffer[delimiter_end:]
            self._release_line(line)

    def _line_boundary(self):
        for index, character in enumerate(self._buffer):
            if character == "\n":
                return index, index + 1
            if character != "\r":
                continue
            if index + 1 == len(self._buffer):
                return None
            return index, index + 2 if self._buffer[index + 1] == "\n" else index + 1
        return None

    def _release_line(self, line):
        if contains_isolated_surrogate(line):
            self._disabled = True
            self._buffer = ""
            return
        line = normalize_surrogate_pairs(line)
        try:
            line_bytes = len(line.encode("utf-8"))
        except (UnicodeEncodeError, UnicodeError):
            line_bytes = MAX_PREVIEW_LINE_BYTES + 1
        if line_bytes > MAX_PREVIEW_LINE_BYTES:
            self._disabled = True
            self._buffer = ""
            return
        try:
            raw_snapshot = "\n".join((self._raw_snapshot, line)).lstrip("\n")
            if len(raw_snapshot.encode("utf-8")) > MAX_PREVIEW_LINE_BYTES:
                raise ValueError("preview snapshot too large")
            projected = sanitize_terminal_security_line(raw_snapshot)
            if _PRIVATE_KEY_BEGIN.search(projected):
                raise ValueError("private key preview")
            safe_snapshot = sanitize_terminal_security_line(
                self._redact_text(projected)
            )
            if _PRIVATE_KEY_BEGIN.search(safe_snapshot) or any(
                value in safe_snapshot for value in self._secret_values
            ):
                raise ValueError("unsafe preview projection")
            snapshot_bytes = len(safe_snapshot.encode("utf-8"))
        except Exception:
            self._disabled = True
            self._buffer = ""
            return
        self._raw_snapshot = raw_snapshot
        if not safe_snapshot or safe_snapshot == self._safe_snapshot:
            return
        if snapshot_bytes > MAX_PREVIEW_LINE_BYTES:
            self._disabled = True
            self._buffer = ""
            return
        callback = self._on_safe_preview
        if not callable(callback):
            self._disabled = True
            return
        try:
            displayed = callback(safe_snapshot) is True
        except Exception:
            displayed = False
        if not displayed:
            self._disabled = True
            return
        self._safe_snapshot = safe_snapshot
        if not self._preview_emitted:
            self._preview_emitted = True
            elapsed = max(0.0, self._clock() - self._started_at)
            self._first_preview_ms = int(elapsed * 1000)
