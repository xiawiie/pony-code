"""Small, safe Markdown rendering for Pony's terminal conversation."""

from __future__ import annotations

import re

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.utils import get_cwidth


_INLINE = re.compile(
    r"(?P<link>\[(?P<label>[^\]\n]+)\]\((?P<url>[^)\s\n]+)\))"
    r"|(?P<code>`[^`\n]+`)"
    r"|(?P<strong>\*\*[^*\n]+\*\*|__[^_\n]+__)"
    r"|(?P<emphasis>(?<!\*)\*[^*\n]+\*(?!\*)|(?<![\w_])_[^_\n]+_(?![\w_]))"
)
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_LIST_ITEM = re.compile(r"^(\s*)(?:(\d+[.)])|[-+*])\s+(.+)$")
_QUOTE = re.compile(r"^ {0,3}>\s?(.*)$")
_RULE = re.compile(r"^ {0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[^`~]*$")
_TABLE_DIVIDER = re.compile(r"^:?-{3,}:?$")


def sanitize_terminal_text(text) -> str:
    """Remove terminal control characters while retaining text layout."""
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        character
        for character in normalized
        if character in "\n\t"
        or (ord(character) >= 32 and not 127 <= ord(character) <= 159)
    )


def _style(base_style, addition=""):
    return " ".join(part for part in (base_style.strip(), addition) if part)


def _append(fragments, style, text):
    if not text:
        return
    if fragments and fragments[-1][0] == style:
        previous_style, previous_text = fragments[-1]
        fragments[-1] = (previous_style, previous_text + text)
    else:
        fragments.append((style, text))


def _inline_fragments(text, base_style):
    fragments = []
    position = 0
    for match in _INLINE.finditer(text):
        _append(fragments, base_style, text[position : match.start()])
        value = match.group(0)
        if match.lastgroup == "code":
            _append(fragments, _style(base_style, "class:markdown.code"), value[1:-1])
        elif match.lastgroup == "strong":
            _append(fragments, _style(base_style, "bold"), value[2:-2])
        elif match.lastgroup == "emphasis":
            _append(fragments, _style(base_style, "italic"), value[1:-1])
        else:
            link_style = _style(base_style, "class:markdown.link underline")
            _append(fragments, link_style, match.group("label"))
            _append(fragments, _style(base_style, "class:markdown.link"), f" ({match.group('url')})")
        position = match.end()
    _append(fragments, base_style, text[position:])
    return fragments


def _width(fragments):
    return sum(get_cwidth(text) for _style_name, text in fragments)


def _prefix_for_width(prefix, width):
    if _width(prefix) < width:
        return prefix
    return []


def _wrap_prose(fragments, width, *, first_prefix=(), continuation_prefix=()):
    lines = []
    prefix = _prefix_for_width(list(first_prefix), width)
    current = list(prefix)
    current_width = _width(current)
    content_width = 0
    pending_space = None

    def next_line():
        nonlocal current, current_width, content_width
        lines.append(current)
        current = list(_prefix_for_width(list(continuation_prefix), width))
        current_width = _width(current)
        content_width = 0

    for style_name, value in fragments:
        for token in re.findall(r"\s+|\S+", value):
            if token.isspace():
                if content_width:
                    pending_space = style_name
                continue
            token_width = get_cwidth(token)
            if (
                pending_space is not None
                and content_width
                and current_width + 1 + token_width > width
            ):
                next_line()
            elif pending_space is not None and content_width:
                _append(current, pending_space, " ")
                current_width += 1
                content_width += 1
            pending_space = None
            for character in token:
                character_width = get_cwidth(character)
                if content_width and current_width + character_width > width:
                    next_line()
                if current_width + character_width > width:
                    character, character_width = "?", 1
                _append(current, style_name, character)
                current_width += character_width
                content_width += character_width
    lines.append(current)
    return lines


def _wrap_code(text, width, base_style):
    code_style = _style(base_style, "class:markdown.code")
    prefix = [(code_style, "  ")] if width > 2 else []
    lines = []
    current = list(prefix)
    current_width = _width(current)
    for character in text:
        character_width = get_cwidth(character)
        if current_width + character_width > width:
            lines.append(current)
            current = list(prefix)
            current_width = _width(current)
        if current_width + character_width > width:
            character, character_width = "?", 1
        _append(current, code_style, character)
        current_width += character_width
    lines.append(current)
    return lines


def _table_cells(line):
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]
    cells = [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", stripped)]
    return cells if len(cells) >= 2 else None


def _table_at(lines, start):
    if start + 1 >= len(lines):
        return None
    header = _table_cells(lines[start])
    divider = _table_cells(lines[start + 1])
    if not header or not divider or len(header) != len(divider):
        return None
    if not all(_TABLE_DIVIDER.fullmatch(cell) for cell in divider):
        return None
    rows = []
    position = start + 2
    while position < len(lines):
        row = _table_cells(lines[position])
        if row is None or len(row) != len(header):
            break
        rows.append(row)
        position += 1
    return header, divider, rows, position


def _aligned_cell(fragments, width, alignment, base_style):
    padding = width - _width(fragments)
    if alignment == "center":
        left = padding // 2
    elif alignment == "right":
        left = padding
    else:
        left = 0
    return [
        (base_style, " " * left),
        *fragments,
        (base_style, " " * (padding - left)),
    ]


def _render_table(header, divider, rows, width, base_style):
    parsed_rows = [
        [_inline_fragments(cell, base_style) for cell in row]
        for row in [header, *rows]
    ]
    column_widths = [max(_width(row[index]) for row in parsed_rows) for index in range(len(header))]
    table_width = sum(column_widths) + 3 * (len(header) - 1)
    if table_width > width:
        return _render_table_fallback(header, rows, width, base_style)

    alignments = [
        "center" if cell.startswith(":") and cell.endswith(":") else "right" if cell.endswith(":") else "left"
        for cell in divider
    ]
    output = []
    for row_index, row in enumerate(parsed_rows):
        line = []
        for index, cell in enumerate(row):
            if row_index == 0:
                cell = [(_style(style_name, "bold"), text) for style_name, text in cell]
            if index:
                _append(line, _style(base_style, "class:markdown.table"), " \u2502 ")
            line.extend(
                _aligned_cell(cell, column_widths[index], alignments[index], base_style)
            )
        output.append(line)
        if row_index == 0:
            rule = "\u2500\u253c\u2500".join("\u2500" * cell_width for cell_width in column_widths)
            output.append([(_style(base_style, "class:markdown.rule"), rule)])
    return output


def _render_table_fallback(header, rows, width, base_style):
    output = []
    for row in rows or [[""] * len(header)]:
        for key, value in zip(header, row, strict=True):
            fragments = _inline_fragments(key, _style(base_style, "bold"))
            _append(fragments, base_style, ": ")
            fragments.extend(_inline_fragments(value, base_style))
            output.extend(_wrap_prose(fragments, width))
    return output


def _fence_end(lines, start, marker):
    closing = re.compile(rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*$")
    for position in range(start + 1, len(lines)):
        if closing.fullmatch(lines[position]):
            return position
    return None


def _emit(output, lines, base_style):
    for line in lines:
        output.extend(line)
        output.append((base_style, "\n"))


def render_markdown(text, *, width, base_style="") -> FormattedText:
    """Render a safe, deliberately small Markdown subset into terminal fragments."""
    width = max(1, int(width))
    source_lines = sanitize_terminal_text(text).split("\n")
    if len(source_lines) > 1 and source_lines[-1] == "":
        source_lines.pop()
    output = []
    position = 0
    while position < len(source_lines):
        line = source_lines[position].expandtabs(4)
        table = _table_at(source_lines, position)
        if table:
            header, divider, rows, position = table
            _emit(output, _render_table(header, divider, rows, width, base_style), base_style)
            continue
        fence = _FENCE.fullmatch(line)
        fence_end = _fence_end(source_lines, position, fence.group(1)) if fence else None
        if fence and fence_end is not None:
            code_lines = source_lines[position + 1 : fence_end]
            for code_line in code_lines or [""]:
                _emit(output, _wrap_code(code_line.expandtabs(4), width, base_style), base_style)
            position = fence_end + 1
            continue
        heading = _HEADING.fullmatch(line)
        quote = _QUOTE.fullmatch(line)
        item = _LIST_ITEM.fullmatch(line)
        if heading:
            fragments = _inline_fragments(heading.group(1), _style(base_style, "bold"))
            rendered_lines = _wrap_prose(fragments, width)
        elif quote:
            prefix = [(_style(base_style, "class:markdown.quote"), "\u2502 ")] if width > 2 else []
            rendered_lines = _wrap_prose(
                _inline_fragments(quote.group(1), base_style),
                width,
                first_prefix=prefix,
                continuation_prefix=prefix,
            )
        elif item:
            marker = f"{item.group(2)} " if item.group(2) else "\u2022 "
            indent = " " * min(len(item.group(1)), max(0, width - get_cwidth(marker) - 1))
            prefix = [(base_style, indent + marker)] if width > get_cwidth(marker) else []
            continuation = [(base_style, " " * _width(prefix))]
            rendered_lines = _wrap_prose(
                _inline_fragments(item.group(3), base_style),
                width,
                first_prefix=prefix,
                continuation_prefix=continuation,
            )
        elif _RULE.fullmatch(line):
            rendered_lines = [[(_style(base_style, "class:markdown.rule"), "\u2500" * width)]]
        elif not line:
            rendered_lines = [[]]
        else:
            rendered_lines = _wrap_prose(_inline_fragments(line, base_style), width)
        _emit(output, rendered_lines, base_style)
        position += 1
    return FormattedText(output)
