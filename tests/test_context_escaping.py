"""Tests for `pico.context.escaping.escape_pico_tags` — prompt-injection
defense that breaks literal `<pico:*>` tag lookalikes with a zero-width
space so user/tool content can't impersonate structural tags."""

from pico.context.escaping import escape_pico_tags


def test_plain_text_unchanged():
    assert escape_pico_tags("hello world") == "hello world"


def test_pico_open_tag_gets_zero_width_space():
    src = "here is <pico:foo>evil</pico:foo>"
    out = escape_pico_tags(src)
    # zero-width space (U+200B) inserted between `pico` and `:`
    assert "<pico​:" in out
    assert "</pico​:" in out
    assert "<pico:" not in out
    assert "</pico:" not in out


def test_visible_length_preserved_ignoring_zwsp():
    src = "<pico:x>"
    out = escape_pico_tags(src)
    assert out.replace("​", "") == src


def test_no_partial_replace_of_similar_prefixes():
    # `<picofoo:tag>` is not `<pico:*>` — must not be touched.
    src = "<picofoo:tag>"
    assert escape_pico_tags(src) == "<picofoo:tag>"


def test_empty_string_returned_unchanged():
    assert escape_pico_tags("") == ""


def test_none_input_returned_as_is():
    # Defensive: some renderers may pass through a Falsy default;
    # we just short-circuit and return the input unchanged.
    assert escape_pico_tags(None) is None


def test_multiple_occurrences_all_escaped():
    src = "<pico:a> body </pico:a> then <pico:b> more </pico:b>"
    out = escape_pico_tags(src)
    assert out.count("<pico​:") == 2
    assert out.count("</pico​:") == 2
    assert "<pico:" not in out
    assert "</pico:" not in out
