"""Tests for `pony.context.escaping.escape_pony_tags` — prompt-injection
defense that breaks literal `<pony:*>` tag lookalikes with a zero-width
space so user/tool content can't impersonate structural tags."""

from pony.context.escaping import escape_pony_tags


def test_plain_text_unchanged():
    assert escape_pony_tags("hello world") == "hello world"


def test_pony_open_tag_gets_zero_width_space():
    src = "here is <pony:foo>evil</pony:foo>"
    out = escape_pony_tags(src)
    # zero-width space (U+200B) inserted between `pony` and `:`
    assert "<pony​:" in out
    assert "</pony​:" in out
    assert "<pony:" not in out
    assert "</pony:" not in out


def test_visible_length_preserved_ignoring_zwsp():
    src = "<pony:x>"
    out = escape_pony_tags(src)
    assert out.replace("​", "") == src


def test_no_partial_replace_of_similar_prefixes():
    # `<ponyfoo:tag>` is not `<pony:*>` — must not be touched.
    src = "<ponyfoo:tag>"
    assert escape_pony_tags(src) == "<ponyfoo:tag>"


def test_empty_string_returned_unchanged():
    assert escape_pony_tags("") == ""


def test_none_input_returned_as_is():
    # Defensive: some renderers may pass through a Falsy default;
    # we just short-circuit and return the input unchanged.
    assert escape_pony_tags(None) is None


def test_multiple_occurrences_all_escaped():
    src = "<pony:a> body </pony:a> then <pony:b> more </pony:b>"
    out = escape_pony_tags(src)
    assert out.count("<pony​:") == 2
    assert out.count("</pony​:") == 2
    assert "<pony:" not in out
    assert "</pony:" not in out
