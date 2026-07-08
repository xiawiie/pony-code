"""Task A2: strip_pico_meta scrubs internal metadata from provider payloads."""

from pico.providers.message_utils import strip_pico_meta


def test_strip_pico_meta_removes_key():
    src = [{"role": "user", "content": "hi", "_pico_meta": {"created_at": "2026-07-08"}}]
    out = strip_pico_meta(src)
    assert out == [{"role": "user", "content": "hi"}]


def test_strip_pico_meta_leaves_role_content_intact():
    src = [
        {"role": "user", "content": "hi", "_pico_meta": {"a": 1}},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}], "_pico_meta": {"b": 2}},
    ]
    out = strip_pico_meta(src)
    assert out[0]["role"] == "user"
    assert out[0]["content"] == "hi"
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == [{"type": "text", "text": "hello"}]


def test_strip_pico_meta_idempotent():
    src = [{"role": "user", "content": "hi"}]
    once = strip_pico_meta(src)
    twice = strip_pico_meta(once)
    assert once == twice == src


def test_strip_pico_meta_does_not_mutate_input():
    src = [{"role": "user", "content": "hi", "_pico_meta": {"a": 1}}]
    strip_pico_meta(src)
    assert src[0]["_pico_meta"] == {"a": 1}


def test_strip_pico_meta_empty_list():
    assert strip_pico_meta([]) == []


def test_no_pico_meta_reaches_anthropic_payload():
    """Anthropic adapter payload must not contain _pico_meta anywhere."""
    from unittest.mock import patch, MagicMock
    import json
    from pico.providers.anthropic_compatible import AnthropicCompatibleModelClient

    client = AnthropicCompatibleModelClient(
        model="claude-3-5-sonnet-latest",
        base_url="https://api.anthropic.com",
        api_key="test",
        temperature=0.0,
        timeout=10,
    )
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = json.loads(req.data.decode("utf-8"))
        m = MagicMock()
        m.__enter__.return_value = MagicMock(
            read=lambda: json.dumps({
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {},
            }).encode("utf-8"),
        )
        m.__exit__.return_value = False
        return m

    with patch("urllib.request.urlopen", fake_urlopen):
        client.complete_v2(
            system=[{"type": "text", "text": "sys"}],
            tools=[],
            messages=[{"role": "user", "content": "hi", "_pico_meta": {"created_at": "x"}}],
            max_tokens=10,
        )
    payload_str = json.dumps(captured["data"])
    assert "_pico_meta" not in payload_str
