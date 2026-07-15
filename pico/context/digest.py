"""Tool result digest — replace long tool outputs with a short summary
that keeps the shape of the result (title + ≤5 bullets + hash pointer)
while stashing the full text to disk for later recovery.

The agent loop calls :func:`should_digest` right after the tool
executor returns a payload. When the payload is small it goes into
``session["messages"]`` verbatim; when it exceeds the threshold it gets
written to ``.pico/runs/<run_id>/tool_results/<hash>.txt`` and a
:class:`ToolResultDigest` (rendered by :func:`render_digest_content`)
replaces the inline content.

**Per-tool summarizers.** Each known tool has a small function that
picks bullets meaningful for that shape (top-level symbols for
``read_file``, exit code + stdout/stderr for ``run_shell``, first
matches for ``grep`` / ``search``). Unknown tools fall through to a
generic "last 3 non-empty lines" summary. Any summarizer that raises
also falls through to the generic path — a broken heuristic must never
break the turn.

The digest is *content-addressed* by the sha256 of the raw result.
``source_hash`` keeps the existing 16-character storage key while
``content_sha256`` exposes the complete digest.  Provider-visible text
uses only that digest and an optional logical ``raw_result_id``; it never
contains the Project State host path used to persist the body.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolResultDigest:
    """A short, structured stand-in for a long tool_result body.

    Fields
    ------
    tool
        Originating tool name.
    title
        Single-line summary (e.g. ``"a.py (30 lines)"``).
    bullets
        Up to five short lines that carry the "shape" of the result —
        symbols for a read_file, exit code + head/tail for a shell, etc.
    source_hash
        First 16 hex chars of ``sha256(result)``; identical results
        share this hash.
    content_sha256
        Complete hexadecimal SHA-256 of the redacted raw result.
    raw_result_id
        Logical identifier for a successfully persisted raw result.  It
        is deliberately not a host filesystem path.
    """

    tool: str
    title: str
    bullets: list = field(default_factory=list)
    source_hash: str = ""
    content_sha256: str = ""
    raw_result_id: str = ""


def should_digest(result, threshold: int = 1200) -> bool:
    """Return True when ``result`` is long enough to warrant a digest."""
    return len(str(result or "")) > threshold


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


# Match top-level Python constructs. Best-effort — the summarizer is
# happy with the first five hits and drops to the "first body line"
# fallback when nothing matches.
_PY_TOP_LEVEL_RE = re.compile(r"^(def |class |import |from )([\w\.]+)", re.M)


def _digest_read_file(args, result):
    path = str(args.get("path") or "unknown")
    line_count = result.count("\n") + 1
    symbols = _PY_TOP_LEVEL_RE.findall(result)[:5]
    bullets = [f"{kind.strip()}{name}" for kind, name in symbols]
    return ToolResultDigest(
        tool="read_file",
        title=f"{path} ({line_count} lines)",
        bullets=bullets or [result.splitlines()[0][:80] if result else ""],
    )


def _digest_run_shell(args, result):
    cmd = str(args.get("command") or "")[:80]
    lines = result.splitlines()
    exit_line = next((line for line in lines if "exit" in line.lower()), "exit_code: ?")
    stdout_lines = [line for line in lines if line and "err" not in line.lower()][:3]
    stderr_lines = [line for line in lines if "err" in line.lower()][-3:]
    return ToolResultDigest(
        tool="run_shell",
        title=f"$ {cmd}",
        bullets=[exit_line] + stdout_lines[:3] + stderr_lines[-3:],
    )


def _digest_grep(args, result):
    pattern = str(args.get("pattern") or "")
    lines = [line for line in result.splitlines() if line.strip()]
    hits = lines[:5]
    return ToolResultDigest(
        tool="grep",
        title=f'grep "{pattern}" → {len(lines)} lines',
        bullets=hits,
    )


def _digest_fallback(tool_name, args, result):
    lines = [line for line in result.splitlines() if line.strip()]
    tail = lines[-3:] if lines else []
    return ToolResultDigest(
        tool=tool_name,
        title=f"{tool_name} result",
        bullets=tail,
    )


_DIGESTERS = {
    "read_file": _digest_read_file,
    "run_shell": _digest_run_shell,
    "grep": _digest_grep,
    "search": _digest_grep,   # search shares grep-shaped output
}


def digest_tool_result(
    tool_name: str,
    args,
    result: str,
    raw_result_id: str = "",
) -> ToolResultDigest:
    """Digest ``result`` and attach its hash and logical raw-result id.

    Any exception from a per-tool summarizer falls through to the
    generic fallback (three-line tail). The digest always carries a
    valid ``source_hash`` so downstream cache lookups by hash keep
    working even on the fallback path.
    """
    fn = _DIGESTERS.get(tool_name)
    if fn is None:
        base = _digest_fallback(tool_name, args or {}, result or "")
    else:
        try:
            base = fn(args or {}, result or "")
        except Exception:
            base = _digest_fallback(tool_name, args or {}, result or "")
    content_sha256 = _hash(result or "")
    return ToolResultDigest(
        tool=base.tool,
        title=base.title,
        bullets=list(base.bullets),
        source_hash=content_sha256[:16],
        content_sha256=content_sha256,
        raw_result_id=raw_result_id,
    )


def render_digest_content(digest: ToolResultDigest) -> str:
    """Format ``digest`` for insertion into a tool_result message."""
    bullet_text = "\n".join(f"- {b}" for b in digest.bullets)
    metadata = [f"content_sha256: sha256:{digest.content_sha256}"]
    if digest.raw_result_id:
        metadata.append(f"raw_result_id: {digest.raw_result_id}")
    return f"[digest] {digest.title}\n{bullet_text}\n" + "\n".join(metadata)
