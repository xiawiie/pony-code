"""Bounded, model-visible tool result pages and previews."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json


MAX_RESULT_PAGE_LINES = 2_000
MAX_RESULT_PAGE_BYTES = 50 * 1024
_RESULT_VIEW_KEYS = {
    "delivery",
    "truncated",
    "start_line",
    "end_line",
    "total_lines",
    "next_start",
    "next_start_byte",
    "reasons",
    "recoverable",
    "exit_code",
}
_RESULT_VIEW_REASONS = {"lines", "bytes", "tokens"}


class ToolResultError(ValueError):
    """Stable failure raised while shaping or recovering a tool result."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True)
class ToolOutput:
    content: str
    result_view: dict


@dataclass(frozen=True)
class ResultPagePolicy:
    max_tokens: int
    token_counter: Callable[[str], int]
    redact_text: Callable[[str], str]
    max_lines: int = MAX_RESULT_PAGE_LINES
    max_bytes: int = MAX_RESULT_PAGE_BYTES

    def __post_init__(self):
        if self.max_lines < 1 or self.max_bytes < 1 or self.max_tokens < 1:
            raise ValueError("invalid result page policy")

    def fits(self, text: str) -> bool:
        return (
            len(text.encode("utf-8")) <= self.max_bytes
            and int(self.token_counter(text)) <= self.max_tokens
        )


def project_result_view(value) -> dict | None:
    """Return the frozen listener-safe result view, or reject it as a unit."""
    if type(value) is not dict or set(value) - _RESULT_VIEW_KEYS:
        return None
    delivery = value.get("delivery")
    truncated = value.get("truncated")
    if delivery not in {"inline", "page", "preview"} or type(truncated) is not bool:
        return None

    reasons = value.get("reasons")
    if reasons is not None and (
        type(reasons) is not list
        or any(type(reason) is not str or reason not in _RESULT_VIEW_REASONS for reason in reasons)
    ):
        return None
    if "exit_code" in value and type(value["exit_code"]) is not int:
        return None

    range_keys = {"start_line", "end_line", "total_lines"}
    continuation_keys = {"next_start", "next_start_byte"}
    present_range = range_keys & set(value)
    present_continuation = continuation_keys & set(value)
    if delivery == "inline":
        if truncated or present_range or present_continuation or "recoverable" in value:
            return None
        if reasons not in (None, []):
            return None
    elif delivery == "preview":
        if (
            not truncated
            or present_range
            or present_continuation
            or type(value.get("recoverable")) is not bool
            or not reasons
        ):
            return None
    else:
        if truncated or present_range != range_keys or "recoverable" in value:
            return None
        if any(
            type(value[key]) is not int or value[key] < 0
            for key in range_keys
        ):
            return None
        if value["start_line"] < 1 or value["end_line"] > value["total_lines"]:
            return None
        if len(present_continuation) > 1:
            return None
        if present_continuation:
            key = next(iter(present_continuation))
            if type(value[key]) is not int or value[key] < 0 or not reasons:
                return None
            if key == "next_start" and value[key] < 1:
                return None
        elif reasons not in (None, []):
            return None
    return dict(value)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_line(marker: str, value: Mapping) -> str:
    payload = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))
    return f"[{marker}] {payload}"


def _byte_offset_to_character(text: str, offset: int) -> int:
    encoded = text.encode("utf-8")
    if offset < 0 or offset > len(encoded):
        raise ToolResultError("tool_result_page_invalid")
    try:
        return len(encoded[:offset].decode("utf-8"))
    except UnicodeDecodeError:
        raise ToolResultError("tool_result_page_invalid") from None


def _limits_exceeded(policy: ResultPagePolicy, text: str) -> list[str]:
    reasons = []
    if len(text.encode("utf-8")) > policy.max_bytes:
        reasons.append("bytes")
    if int(policy.token_counter(text)) > policy.max_tokens:
        reasons.append("tokens")
    return reasons


def _render_page(
    *,
    locator: Mapping,
    source_hash: str,
    start: int,
    start_byte: int | None,
    end_line: int,
    total_lines: int,
    requested_end: int | None,
    body: str,
    next_line: int | None,
    next_byte: int | None,
    reasons: list[str],
) -> str:
    range_complete = next_line is None
    file_complete = range_complete and (
        requested_end is None or requested_end >= total_lines
    )
    header = {
        **dict(locator),
        "start": start,
        "end": end_line,
        "total_lines": total_lines,
        "range_complete": range_complete,
        "file_complete": file_complete,
        "sha256": source_hash,
    }
    if start_byte is not None:
        header["start_byte"] = start_byte
    rendered = _json_line("page", header)
    if body:
        rendered += "\n" + body
    if next_line is not None:
        continuation = {
            **dict(locator),
            "start": next_line,
            "expected_sha256": source_hash,
        }
        if next_byte is not None:
            continuation["start_byte"] = next_byte
        if requested_end is not None:
            continuation["end"] = requested_end
        if reasons:
            continuation["reasons"] = reasons
        if not rendered.endswith(("\n", "\r")):
            rendered += "\n"
        rendered += _json_line("continuation", continuation)
    return rendered


def _page_view(
    *,
    start: int,
    end_line: int,
    total_lines: int,
    next_line: int | None,
    next_byte: int | None,
    reasons: list[str],
) -> dict:
    view = {
        "delivery": "page",
        "truncated": False,
        "start_line": start,
        "end_line": end_line,
        "total_lines": total_lines,
    }
    if next_line is not None:
        if next_byte is None:
            view["next_start"] = next_line
        else:
            view["next_start_byte"] = next_byte
        view["reasons"] = list(reasons)
    return view


def page_text(
    source: str,
    *,
    locator: Mapping,
    policy: ResultPagePolicy,
    start: int = 1,
    end: int | None = None,
    start_byte: int | None = None,
    expected_sha256: str | None = None,
) -> ToolOutput:
    """Return one lossless page of a redacted UTF-8 text source."""
    if start < 1 or (end is not None and end < start):
        raise ToolResultError("tool_result_page_invalid")
    if start_byte is not None and expected_sha256 is None:
        raise ToolResultError("tool_result_page_invalid")
    safe_source = str(policy.redact_text(source))
    safe_locator = {
        str(key): (
            str(policy.redact_text(value)) if isinstance(value, str) else value
        )
        for key, value in dict(locator).items()
    }
    source_hash = _sha256(safe_source)
    if expected_sha256 is not None and expected_sha256 != source_hash:
        raise ToolResultError("tool_result_source_changed")

    lines = safe_source.splitlines(keepends=True)
    total_lines = len(lines)
    last_requested = total_lines if end is None else min(end, total_lines)
    if start > last_requested:
        if start_byte not in (None, 0):
            raise ToolResultError("tool_result_page_invalid")
        rendered = _render_page(
            locator=safe_locator,
            source_hash=source_hash,
            start=start,
            start_byte=start_byte,
            end_line=max(0, min(total_lines, start - 1)),
            total_lines=total_lines,
            requested_end=end,
            body="",
            next_line=None,
            next_byte=None,
            reasons=[],
        )
        if not policy.fits(rendered):
            raise ToolResultError("tool_result_budget_too_small")
        return ToolOutput(
            rendered,
            _page_view(
                start=start,
                end_line=max(0, min(total_lines, start - 1)),
                total_lines=total_lines,
                next_line=None,
                next_byte=None,
                reasons=[],
            ),
        )

    line_index = start - 1
    first_line = lines[line_index]
    character_offset = _byte_offset_to_character(first_line, start_byte or 0)
    if character_offset == len(first_line):
        if line_index + 1 >= last_requested:
            rendered = _render_page(
                locator=safe_locator,
                source_hash=source_hash,
                start=start,
                start_byte=start_byte,
                end_line=start,
                total_lines=total_lines,
                requested_end=end,
                body="",
                next_line=None,
                next_byte=None,
                reasons=[],
            )
            if not policy.fits(rendered):
                raise ToolResultError("tool_result_budget_too_small")
            return ToolOutput(
                rendered,
                _page_view(
                    start=start,
                    end_line=start,
                    total_lines=total_lines,
                    next_line=None,
                    next_byte=None,
                    reasons=[],
                ),
            )
        line_index += 1
        start += 1
        start_byte = None
        character_offset = 0

    accepted: list[str] = []
    accepted_lines = 0
    cursor = line_index
    while cursor < last_requested and accepted_lines < policy.max_lines:
        candidate_piece = lines[cursor]
        if cursor == line_index and character_offset:
            candidate_piece = candidate_piece[character_offset:]
        candidate_body = "".join([*accepted, candidate_piece])
        candidate_next = cursor + 2 if cursor + 1 < last_requested else None
        candidate = _render_page(
            locator=safe_locator,
            source_hash=source_hash,
            start=start,
            start_byte=start_byte,
            end_line=cursor + 1,
            total_lines=total_lines,
            requested_end=end,
            body=candidate_body,
            next_line=candidate_next,
            next_byte=None,
            reasons=[],
        )
        if not policy.fits(candidate):
            break
        accepted.append(candidate_piece)
        accepted_lines += 1
        cursor += 1

    if cursor >= last_requested:
        next_line = None
        reasons: list[str] = []
    elif accepted_lines >= policy.max_lines:
        next_line = cursor + 1
        reasons = ["lines"]
    else:
        failed_body = "".join([*accepted, lines[cursor]])
        failed = _render_page(
            locator=safe_locator,
            source_hash=source_hash,
            start=start,
            start_byte=start_byte,
            end_line=cursor + 1,
            total_lines=total_lines,
            requested_end=end,
            body=failed_body,
            next_line=cursor + 2 if cursor + 1 < last_requested else None,
            next_byte=None,
            reasons=[],
        )
        reasons = _limits_exceeded(policy, failed) or ["tokens"]
        next_line = cursor + 1

    if accepted:
        body = "".join(accepted)
        end_line = cursor
        rendered = _render_page(
            locator=safe_locator,
            source_hash=source_hash,
            start=start,
            start_byte=start_byte,
            end_line=end_line,
            total_lines=total_lines,
            requested_end=end,
            body=body,
            next_line=next_line,
            next_byte=None,
            reasons=reasons,
        )
        while accepted and not policy.fits(rendered):
            for reason in _limits_exceeded(policy, rendered):
                if reason not in reasons:
                    reasons.append(reason)
            accepted.pop()
            cursor -= 1
            end_line -= 1
            next_line = cursor + 1
            body = "".join(accepted)
            rendered = _render_page(
                locator=safe_locator,
                source_hash=source_hash,
                start=start,
                start_byte=start_byte,
                end_line=end_line,
                total_lines=total_lines,
                requested_end=end,
                body=body,
                next_line=next_line,
                next_byte=None,
                reasons=reasons,
            )
        if accepted:
            return ToolOutput(
                rendered,
                _page_view(
                    start=start,
                    end_line=end_line,
                    total_lines=total_lines,
                    next_line=next_line,
                    next_byte=None,
                    reasons=reasons,
                ),
            )

    line = lines[line_index]
    remainder = line[character_offset:]
    offset_bytes = len(line[:character_offset].encode("utf-8"))
    low, high = 0, len(remainder)
    best = ""
    best_rendered = ""
    while low <= high:
        middle = (low + high) // 2
        fragment = remainder[:middle]
        consumed_bytes = len(fragment.encode("utf-8"))
        fragment_rendered = _render_page(
            locator=safe_locator,
            source_hash=source_hash,
            start=start,
            start_byte=start_byte,
            end_line=start,
            total_lines=total_lines,
            requested_end=end,
            body=fragment,
            next_line=start,
            next_byte=offset_bytes + consumed_bytes,
            reasons=reasons,
        )
        if fragment and policy.fits(fragment_rendered):
            best = fragment
            best_rendered = fragment_rendered
            low = middle + 1
        else:
            high = middle - 1
    if not best:
        raise ToolResultError("tool_result_budget_too_small")
    next_byte = offset_bytes + len(best.encode("utf-8"))
    return ToolOutput(
        best_rendered,
        _page_view(
            start=start,
            end_line=start,
            total_lines=total_lines,
            next_line=start,
            next_byte=next_byte,
            reasons=reasons,
        ),
    )


def render_overflow_preview(
    content: str,
    *,
    content_sha256: str,
    raw_result_id: str | None,
    max_tokens: int,
    token_counter: Callable[[str], int],
) -> str:
    """Render a bounded head-tail preview with an honest recovery marker."""
    recoverable = raw_result_id is not None
    footer = (
        f"[result] content_sha256=sha256:{content_sha256} "
        f"recoverable={'true' if recoverable else 'false'} "
        "scope=current_run expires=end_of_turn"
    )
    if raw_result_id is not None:
        footer += f" raw_result_id={raw_result_id}"
    prefix = "[preview] output truncated"
    if int(token_counter(f"{prefix}\n{footer}")) > max_tokens:
        raise ToolResultError("tool_result_budget_too_small")
    low, high = 0, len(content)
    best = ""
    while low <= high:
        size = (low + high) // 2
        head_size = (size + 1) // 2
        tail_size = size // 2
        head = content[:head_size]
        tail = content[len(content) - tail_size :] if tail_size else ""
        candidate = f"{prefix}\n{head}\n[... omitted ...]\n{tail}\n{footer}"
        if int(token_counter(candidate)) <= max_tokens:
            best = candidate
            low = size + 1
        else:
            high = size - 1
    return best or f"{prefix}\n{footer}"
