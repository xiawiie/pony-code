import hashlib
import json

import pytest

from pony.tools.result_view import (
    ResultPagePolicy,
    ToolResultError,
    page_text,
    project_result_view,
)


def _policy(*, max_bytes=50 * 1024, max_tokens=100_000, token_counter=None):
    return ResultPagePolicy(
        max_tokens=max_tokens,
        token_counter=token_counter or (lambda text: len(text) // 4),
        redact_text=str,
        max_bytes=max_bytes,
    )


def _marker(output, name):
    prefix = f"[{name}] "
    line = next(line for line in output.content.splitlines() if line.startswith(prefix))
    return json.loads(line[len(prefix) :])


@pytest.mark.parametrize("source", ("a\nb\n", "a\r\nb\r\n", "a\nb"))
def test_complete_page_preserves_source_newlines(source):
    output = page_text(source, locator={"path": "a.txt"}, policy=_policy())

    body = output.content.split("\n", 1)[1]
    assert body == source
    assert output.result_view == {
        "delivery": "page",
        "truncated": False,
        "start_line": 1,
        "end_line": 2,
        "total_lines": 2,
    }
    assert _marker(output, "page")["sha256"] == hashlib.sha256(
        source.encode("utf-8")
    ).hexdigest()


def test_page_stops_at_two_thousand_complete_lines():
    source = "".join(f"line {number}\n" for number in range(1, 3_001))

    output = page_text(source, locator={"path": "many.txt"}, policy=_policy())

    continuation = _marker(output, "continuation")
    assert output.result_view["end_line"] == 2_000
    assert output.result_view["next_start"] == 2_001
    assert output.result_view["reasons"] == ["lines"]
    assert continuation["start"] == 2_001
    assert continuation["expected_sha256"] == _marker(output, "page")["sha256"]


def test_page_reports_envelope_limit_that_forces_line_limit_backoff():
    source = "x\n" * 2_001
    line_limited = page_text(source, locator={"path": "many.txt"}, policy=_policy())
    max_bytes = len(line_limited.content.encode("utf-8")) - 1

    output = page_text(
        source,
        locator={"path": "many.txt"},
        policy=_policy(max_bytes=max_bytes),
    )

    assert output.result_view["end_line"] < 2_000
    assert output.result_view["reasons"] == ["lines", "bytes"]
    assert len(output.content.encode("utf-8")) <= max_bytes


def test_page_enforces_final_utf8_byte_limit():
    source = "".join(("x" * 100) + "\n" for _ in range(1_000))

    output = page_text(source, locator={"path": "wide.txt"}, policy=_policy())

    assert len(output.content.encode("utf-8")) <= 50 * 1024
    assert "bytes" in output.result_view["reasons"]
    assert output.result_view["next_start"] > 1


def test_page_enforces_final_token_limit():
    source = "".join(("token" * 20) + "\n" for _ in range(100))
    policy = _policy(max_tokens=2_000, token_counter=len)

    output = page_text(source, locator={"path": "tokens.txt"}, policy=policy)

    assert len(output.content) <= 2_000
    assert "tokens" in output.result_view["reasons"]


def test_long_cjk_line_continuation_uses_utf8_boundary_and_advances():
    source = "界" * 2_000
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    policy = _policy(max_bytes=800)

    first = page_text(source, locator={"path": "cjk.txt"}, policy=policy)
    offset = first.result_view["next_start_byte"]

    assert 0 < offset < len(source.encode("utf-8"))
    source.encode("utf-8")[:offset].decode("utf-8")
    second = page_text(
        source,
        locator={"path": "cjk.txt"},
        policy=policy,
        start=1,
        start_byte=offset,
        expected_sha256=digest,
    )
    assert second.result_view["next_start_byte"] > offset
    assert len(first.content.encode("utf-8")) <= 800
    assert len(second.content.encode("utf-8")) <= 800


def test_page_rejects_mid_codepoint_start_byte():
    source = "界" * 100
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()

    with pytest.raises(ToolResultError, match="tool_result_page_invalid"):
        page_text(
            source,
            locator={"path": "cjk.txt"},
            policy=_policy(),
            start_byte=1,
            expected_sha256=digest,
        )


def test_start_byte_at_line_end_advances_to_the_next_line():
    source = "界\r\nnext\n"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()

    output = page_text(
        source,
        locator={"path": "cjk.txt"},
        policy=_policy(),
        start=1,
        start_byte=len("界\r\n".encode("utf-8")),
        expected_sha256=digest,
    )

    assert _marker(output, "page")["start"] == 2
    assert output.content.endswith("next\n")


def test_start_byte_at_final_line_end_returns_an_empty_complete_page():
    source = "界"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()

    output = page_text(
        source,
        locator={"path": "cjk.txt"},
        policy=_policy(),
        start=1,
        start_byte=len(source.encode("utf-8")),
        expected_sha256=digest,
    )

    assert output.content.count("\n") == 0
    assert output.result_view["end_line"] == 1
    assert "next_start" not in output.result_view
    assert "next_start_byte" not in output.result_view


def test_page_rejects_source_drift_between_pages():
    with pytest.raises(ToolResultError, match="tool_result_source_changed"):
        page_text(
            "changed\n",
            locator={"path": "a.txt"},
            policy=_policy(),
            start=2,
            expected_sha256="0" * 64,
        )


def test_page_hashes_and_limits_the_redacted_visible_source():
    secret = "known-secret-value"
    safe = "prefix <redacted> suffix\n"
    policy = ResultPagePolicy(
        max_tokens=1_000,
        token_counter=len,
        redact_text=lambda text: text.replace(secret, "<redacted>"),
    )

    output = page_text(
        f"prefix {secret} suffix\n",
        locator={"path": "safe.txt"},
        policy=policy,
    )

    assert secret not in output.content
    assert _marker(output, "page")["sha256"] == hashlib.sha256(
        safe.encode("utf-8")
    ).hexdigest()


def test_page_redacts_string_locator_values():
    secret = "known-secret-value"
    policy = ResultPagePolicy(
        max_tokens=1_000,
        token_counter=len,
        redact_text=lambda text: text.replace(secret, "<redacted>"),
    )

    output = page_text(
        "safe\n",
        locator={"path": f"notes/{secret}.md"},
        policy=policy,
    )

    assert secret not in output.content
    assert _marker(output, "page")["path"] == "notes/<redacted>.md"


def test_result_view_projection_rejects_unknown_keys_and_invalid_combinations():
    valid = {
        "delivery": "page",
        "truncated": False,
        "start_line": 1,
        "end_line": 20,
        "total_lines": 40,
        "next_start": 21,
        "reasons": ["lines"],
    }

    assert project_result_view(valid) == valid
    assert project_result_view({**valid, "path": "private.txt"}) is None
    assert project_result_view({**valid, "next_start_byte": 200}) is None
    assert project_result_view({**valid, "reasons": ["unknown"]}) is None
    assert project_result_view(
        {"delivery": "preview", "truncated": True, "reasons": ["tokens"]}
    ) is None


def test_page_fails_when_the_minimum_envelope_cannot_fit():
    with pytest.raises(ToolResultError, match="tool_result_budget_too_small"):
        page_text(
            "content\n",
            locator={"path": "a.txt"},
            policy=_policy(max_tokens=1, token_counter=len),
        )


def test_explicit_range_can_complete_before_eof():
    output = page_text(
        "one\ntwo\nthree\n",
        locator={"path": "a.txt"},
        policy=_policy(),
        start=2,
        end=2,
    )

    header = _marker(output, "page")
    assert header["range_complete"] is True
    assert header["file_complete"] is False
    assert "[continuation]" not in output.content
