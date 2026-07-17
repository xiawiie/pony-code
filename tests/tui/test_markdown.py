from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.utils import get_cwidth

from pony.tui.markdown import render_markdown, sanitize_terminal_text


def _text(fragments):
    return "".join(text for _style, text in fragments)


def _assert_bounded(fragments, width):
    rendered = _text(fragments)
    assert rendered.endswith("\n")
    assert all(get_cwidth(line) <= width for line in rendered.splitlines())


def test_terminal_text_removes_control_characters_but_keeps_layout():
    value = "safe\x1b[31m\r\n\t\u4e2d\x00\x07\x7f\x85end"

    assert sanitize_terminal_text(value) == "safe[31m\n\t\u4e2dend"


def test_markdown_renders_requested_blocks_and_inline_styles():
    source = """# \u6838\u5fc3\u80fd\u529b

\u6b63\u6587 **\u7c97\u4f53**\u3001*\u659c\u4f53*\u3001`code` \u548c [\u6587\u6863](https://example.test)
- \u9605\u8bfb\u4ee3\u7801
> \u5b89\u5168\u8fb9\u754c
---
```python
print("\u4e2d\ud83d\ude42")
```
"""

    rendered = render_markdown(source, width=40, base_style="class:answer")
    text = _text(rendered)
    styles = {style for style, value in rendered if value.strip()}

    assert isinstance(rendered, FormattedText)
    assert "\u6838\u5fc3\u80fd\u529b" in text
    assert "**\u7c97\u4f53**" not in text
    assert "*\u659c\u4f53*" not in text
    assert "`code`" not in text
    assert "\u6587\u6863" in text and "(https://example.test)" in text
    assert "\u2022 \u9605\u8bfb\u4ee3\u7801" in text
    assert "\u2502 \u5b89\u5168\u8fb9\u754c" in text
    assert "print(\"\u4e2d\ud83d\ude42\")" in text
    assert any("bold" in style for style in styles)
    assert any("italic" in style for style in styles)
    assert any("class:markdown.code" in style for style in styles)
    assert all(style.startswith("class:answer") for style in styles)
    _assert_bounded(rendered, 40)


def test_cjk_and_emoji_wrap_by_terminal_cell_width():
    rendered = render_markdown(
        "\u4e2d\u6587\ud83d\ude42 mixed words that wrap safely without terminal overflow",
        width=12,
    )

    _assert_bounded(rendered, 12)
    assert _text(rendered).replace("\n", "").replace(" ", "") == (
        "\u4e2d\u6587\ud83d\ude42mixedwordsthatwrapsafelywithoutterminaloverflow"
    )


def test_pipe_table_aligns_when_it_fits():
    rendered = render_markdown(
        "\u80fd\u529b | \u8bf4\u660e\n--- | ---\n\u8bfb\u4ee3\u7801 | \u5206\u6790\n\u6539\u4ee3\u7801 | \u91cd\u6784",
        width=32,
    )
    text = _text(rendered)

    assert "\u80fd\u529b" in text
    assert "\u8bfb\u4ee3\u7801" in text
    assert "\u2500" in text and "\u253c" in text
    _assert_bounded(rendered, 32)


def test_wide_pipe_table_falls_back_to_key_value_lines():
    rendered = render_markdown(
        "A | B | C\n--- | --- | ---\none | two | three",
        width=12,
    )
    text = _text(rendered)

    assert "A: one" in text
    assert "B: two" in text
    assert "C: three" in text
    assert "\u253c" not in text
    _assert_bounded(rendered, 12)


def test_malformed_markdown_stays_readable():
    source = "before **broken\n```python\nprint('x')"
    rendered = render_markdown(source, width=24)
    text = _text(rendered)

    assert "before **broken" in text
    assert "```python" in text
    assert "print('x')" in text
    _assert_bounded(rendered, 24)
