from pony.agent.streaming import MAX_PREVIEW_LINE_BYTES, SafeTextPreview


def _preview(*, secrets=(), sink=None, clock=lambda: 1.0, redact_text=None):
    return SafeTextPreview(
        redact_text=redact_text
        or (lambda text: text.replace("secret-value", "<redacted>")),
        secret_values=secrets,
        on_safe_preview=sink,
        clock=clock,
    )


def test_preview_waits_for_a_complete_line_and_redacts_cross_chunk_secret():
    displayed = []
    preview = _preview(
        secrets=("secret-value",),
        sink=lambda text: displayed.append(text) or True,
    )

    preview.stream_committed()
    preview.text_delta("value=secret-")
    preview.text_delta("value")
    assert displayed == []
    preview.text_delta("\n")

    assert displayed == ["value=<redacted>"]


def test_preview_snapshot_accumulates_complete_safe_lines():
    displayed = []
    preview = _preview(sink=lambda text: displayed.append(text) or True)

    preview.stream_committed()
    preview.text_delta("first\nsecond\n")

    assert displayed == ["first", "first second"]


def test_preview_redacts_secrets_reassembled_by_terminal_projection():
    for chunks in (
        ("secret-\x00value\n",),
        ("secret-\u200bvalue\n",),
        ("secret-\u202evalue\n",),
        ("secret-\ufe0fvalue\n",),
        ("secret-\u0301value\n",),
    ):
        displayed = []
        preview = _preview(
            secrets=("secret-value",),
            sink=lambda text: displayed.append(text) or True,
        )

        preview.stream_committed()
        for chunk in chunks:
            preview.text_delta(chunk)

        assert displayed == ["<redacted>"]


def test_preview_redacts_secrets_reassembled_from_tabs_or_lines():
    def redact(text):
        return text.replace("secret value", "<redacted>")

    for content in ("secret\tvalue\n", "secret\nvalue\n"):
        displayed = []
        preview = _preview(
            secrets=("secret value",),
            sink=lambda text: displayed.append(text) or True,
            redact_text=redact,
        )

        preview.stream_committed()
        preview.text_delta(content)

        assert displayed[-1] == "<redacted>"
        assert "secret value" not in displayed


def test_preview_fail_closed_for_short_or_multiline_known_secret():
    for secret in ("short", "line\r\nsecret-value"):
        displayed = []
        preview = _preview(
            secrets=(secret,),
            sink=lambda text: displayed.append(text) or True,
        )

        preview.stream_committed()
        preview.text_delta("safe line\n")

        assert displayed == []


def test_preview_private_key_marker_disables_remaining_preview():
    displayed = []
    preview = _preview(sink=lambda text: displayed.append(text) or True)

    preview.stream_committed()
    preview.text_delta("-----BEGIN RSA PRIVATE KEY-----\nvisible later\n")

    assert displayed == []


def test_preview_private_key_marker_cannot_hide_in_terminal_whitespace():
    for content in (
        "-----BEGIN\tRSA PRIVATE KEY-----\n",
        "-----BEGIN\nRSA PRIVATE KEY-----\n",
        "-----BEGIN\ufe0f RSA PRIVATE KEY-----\n",
    ):
        displayed = []
        preview = _preview(sink=lambda text: displayed.append(text) or True)

        preview.stream_committed()
        preview.text_delta(content)

        assert "-----BEGIN RSA PRIVATE KEY-----" not in displayed


def test_preview_fail_closed_when_a_known_secret_changes_during_projection():
    displayed = []
    preview = _preview(
        secrets=("secret-\u200bvalue",),
        sink=lambda text: displayed.append(text) or True,
    )

    preview.stream_committed()
    preview.text_delta("safe line\n")

    assert displayed == []


def test_format_only_preview_does_not_acknowledge_ttft():
    preview = _preview(sink=lambda _text: True)

    preview.stream_committed()
    preview.text_delta("\u200b\u202e\ufe0f\u0301\n")

    assert preview.metadata()["preview_emitted"] is False


def test_preview_sanitizes_controls_and_normalizes_to_one_line():
    displayed = []
    preview = _preview(sink=lambda text: displayed.append(text) or True)

    preview.stream_committed()
    preview.text_delta("safe\x1b[31m\ttext\n")

    assert displayed == ["safe[31m text"]


def test_preview_disables_a_line_over_64_kib_without_affecting_commit():
    displayed = []
    preview = _preview(sink=lambda text: displayed.append(text) or True)

    preview.stream_committed()
    preview.text_delta("x" * (MAX_PREVIEW_LINE_BYTES + 1))
    preview.text_delta("\nsafe later\n")

    assert displayed == []
    assert preview.metadata() == {
        "requested": True,
        "committed": True,
        "preview_emitted": False,
        "first_preview_ms": None,
    }


def test_preview_latency_requires_a_visible_sink_acknowledgment():
    ticks = iter((10.0, 10.125))
    preview = _preview(sink=lambda _text: True, clock=lambda: next(ticks))

    preview.stream_committed()
    preview.text_delta("visible\n")

    assert preview.metadata() == {
        "requested": True,
        "committed": True,
        "preview_emitted": True,
        "first_preview_ms": 125,
    }


def test_preview_sink_failure_disables_remaining_preview():
    calls = []
    preview = _preview(sink=lambda text: calls.append(text) or False)

    preview.stream_committed()
    preview.text_delta("first\nsecond\n")

    assert calls == ["first"]
    assert preview.metadata()["preview_emitted"] is False


def test_unpaired_surrogate_disables_preview_without_raising():
    calls = []
    preview = _preview(sink=lambda text: calls.append(text) or True)

    preview.stream_committed()
    preview.text_delta("unsafe \ud800\n")
    preview.text_delta("safe later\n")

    assert calls == []
    assert preview.metadata()["committed"] is True


def test_valid_surrogate_pair_is_normalized_for_preview():
    calls = []
    preview = _preview(sink=lambda text: calls.append(text) or True)

    preview.stream_committed()
    preview.text_delta("emoji \ud83d\ude42\n")

    assert calls == ["emoji \U0001f642"]
