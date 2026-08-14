from pony.agent.streaming import MAX_PREVIEW_LINE_BYTES, SafeTextPreview


def _preview(*, secrets=(), sink=None, clock=lambda: 1.0):
    return SafeTextPreview(
        redact_text=lambda text: text.replace("secret-value", "<redacted>"),
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

    assert displayed == ["first", "first\nsecond"]


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
