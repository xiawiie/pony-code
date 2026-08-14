"""Request-scoped safe projection for transient streaming text."""

import re
import time

from pony.security.redaction import MIN_SECRET_SUBSTRING_REDACTION_LENGTH
from pony.security.text import sanitize_terminal_line


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
        self._snapshot = ""
        self._disabled = any(
            "\r" in value
            or "\n" in value
            or len(value) < MIN_SECRET_SUBSTRING_REDACTION_LENGTH
            for value in tuple(str(value) for value in secret_values if value)
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
        try:
            line_bytes = len(line.encode("utf-8"))
        except (UnicodeEncodeError, UnicodeError):
            line_bytes = MAX_PREVIEW_LINE_BYTES + 1
        if line_bytes > MAX_PREVIEW_LINE_BYTES or _PRIVATE_KEY_BEGIN.search(line):
            self._disabled = True
            self._buffer = ""
            return
        try:
            safe_line = sanitize_terminal_line(self._redact_text(line))
            snapshot = "\n".join(part for part in (self._snapshot, safe_line) if part)
            snapshot_bytes = len(snapshot.encode("utf-8"))
        except Exception:
            self._disabled = True
            self._buffer = ""
            return
        if not safe_line:
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
            displayed = callback(snapshot) is True
        except Exception:
            displayed = False
        if not displayed:
            self._disabled = True
            return
        self._snapshot = snapshot
        if not self._preview_emitted:
            self._preview_emitted = True
            elapsed = max(0.0, self._clock() - self._started_at)
            self._first_preview_ms = int(elapsed * 1000)
