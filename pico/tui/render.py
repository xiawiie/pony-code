"""Small deterministic renderers for Pico's interactive terminal surface."""

from __future__ import annotations

import json
import shutil
from importlib import metadata
from pathlib import Path

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth


# Terminal-scale adaptations of the horse silhouette selected for Pico's TUI.
_HORSE_LINES = (
    "  ⣶⡄⣷⡄⣄",
    " ⢀⣼⣿⣿⣿⣿⣻⣦⣀",
    " ⣼⣿⣾⣿⣿⣿⣿⣽⣯⣄",
    "⣾⣿⣿⠿⠋⣿⣿⣿⣿⣷⣿⡁  ⢀⣤⣤⣤ ⢀⣤⣄",
    "⠘⠛⠃  ⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣟⡻⣿⣷⡄",
    "    ⢰⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⣿⣿⡇",
    "    ⠈⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠿⣿⣿⣿⡏⠁⢿⣿⣷",
    "  ⢀⣠⣾⣿⠿⢾⣿⡟⠛⠛⠛⠁⠿⣿⣿⡻⣿⣷⡀ ⠘⠁",
    "  ⠘⣿⠉⠁ ⠘⣿⠇     ⣉⣿⡿⠉⢿⣿",
    "   ⢿⣿⣤  ⣿⡇    ⢠⣿⠟⠁ ⢸⣿",
    "    ⠙⠛ ⣼⣿⠃   ⢠⣿⡟  ⣴⣿⠛",
)

_MEDIUM_HORSE_LINES = (
    "   ⣶⡄⣷⣄",
    "  ⣼⣿⣿⣿⣻⣦⣀",
    " ⣾⠿⣿⣿⣿⣷⣿⣤⣤⣄",
    "⠛⠃ ⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦",
    "   ⣿⣿⣿⠿⠛⣿⣿⣿⡇",
    "  ⣼⣿⠃    ⢸⣿⣆",
    "  ⠛⠁     ⠛⠃",
)

_MICRO_HORSE_LINES = (
    "  ⣶⡄⣷⣄",
    " ⣼⣿⣿⣿⣻⣦⣀",
    "⠛⠃⣿⣿⣿⣿⣿⣿⣿⣿⣦",
    "  ⣿⠛⣿⣿⡇ ⣿",
    " ⠛  ⠛  ⠛",
)

_PIXEL_GLYPHS = {
    "P": ("### ", "#  #", "### ", "#   ", "#   "),
    "O": (" ## ", "#  #", "#  #", "#  #", " ## "),
    "N": ("#  #", "## #", "####", "# ##", "#  #"),
    "Y": ("#  #", " ## ", "  # ", "  # ", "  # "),
    "C": (" ###", "#   ", "#   ", "#   ", " ###"),
    "D": ("### ", "#  #", "#  #", "#  #", "### "),
    "E": ("####", "#   ", "### ", "#   ", "####"),
}

_HALF_BLOCKS = {"  ": " ", "# ": "▌", " #": "▐", "##": "█"}
_LARGE_BANNER_COLUMNS = 112
_MEDIUM_BANNER_COLUMNS = 64
_PRODUCT_DESCRIPTION = "Local coding agent for repository-grounded work"

_PROTOCOL_PROVIDERS = {
    "anthropic_messages": "anthropic",
    "openai_responses": "openai",
    "openai_chat_completions": "openai",
    "ollama_chat": "ollama",
}

_COLOR_STYLE = Style.from_dict(
    {
        "logo": "bold",
        "meta": "#858585",
        "editor.prompt": "",
        "editor.border": "#777777",
        "user": "bg:#30303d #f4f4f5",
        "activity": "italic #858585",
        "tool": "bg:#2b2b2b #d7d7d7",
        "tool.success": "bold bg:#2b2b2b #f0f0f0",
        "success": "#3fb950",
        "warning": "bold #d29922",
        "error": "bold #ff4d4f",
        "key": "bold",
        "footer": "#777777",
        "bottom-toolbar": "noreverse",
        "bottom-toolbar.text": "#777777",
        "completion-menu.completion": "bg:#25252d #d4d4d4",
        "completion-menu.completion.current": "bg:#4a4a4a #ffffff",
        "completion-menu.meta.completion": "bg:#25252d #858585",
        "completion-menu.meta.completion.current": "bg:#4a4a4a #ffffff",
    }
)

_PLAIN_STYLE = Style.from_dict(
    {
        "logo": "bold",
        "editor.prompt": "bold",
        "editor.border": "",
        "user": "",
        "activity": "italic",
        "tool": "",
        "tool.success": "bold",
        "warning": "bold",
        "error": "bold",
        "key": "bold",
        "footer": "",
        "bottom-toolbar": "noreverse",
        "bottom-toolbar.text": "",
    }
)


def _pixel_row(pattern, scale):
    if scale == 2:
        return "".join("██" if pixel == "#" else "  " for pixel in pattern)
    if scale == 1:
        return pattern.replace("#", "█")
    return "".join(_HALF_BLOCKS[pattern[index : index + 2]] for index in (0, 2))


def _wordmark_lines(scale, repeats, letter_gap, word_gap):
    lines = []
    for row, repeat in enumerate(repeats):
        words = []
        for word in ("PONY", "CODE"):
            words.append(
                (" " * letter_gap).join(
                    _pixel_row(_PIXEL_GLYPHS[letter][row], scale)
                    for letter in word
                )
            )
        lines.extend([(words[0] + " " * word_gap + words[1]).rstrip()] * repeat)
    return tuple(lines)


def _banner_variant(columns):
    if columns >= _LARGE_BANNER_COLUMNS:
        return _HORSE_LINES, _wordmark_lines(2, (2, 2, 3, 2, 2), 2, 4)
    if columns >= _MEDIUM_BANNER_COLUMNS:
        return _MEDIUM_HORSE_LINES, _wordmark_lines(1, (2, 1, 1, 1, 2), 1, 2)
    return _MICRO_HORSE_LINES, _wordmark_lines(0, (1, 1, 1, 1, 1), 1, 2)


def _banner_lines(columns):
    width = max(1, int(columns) - 1)
    horse_lines, wordmark_lines = _banner_variant(columns)

    horse_width = max(get_cwidth(line) for line in horse_lines)
    wordmark_width = max(get_cwidth(line) for line in wordmark_lines)
    gap = min(3, max(1, width - horse_width - wordmark_width))
    banner_width = horse_width + gap + wordmark_width
    indent = " " * max(0, (width - banner_width) // 2)
    return tuple(
        (
            indent
            + horse
            + " " * (horse_width - get_cwidth(horse) + gap)
            + pony
        ).rstrip()
        for horse, pony in zip(horse_lines, wordmark_lines, strict=True)
    )


def logo_text(columns=80):
    """Return the responsive, color-independent terminal logo."""
    return "\n".join(_banner_lines(columns))


def _logo_fragments(columns):
    return FormattedText(
        [("class:logo", f"{line}\n") for line in _banner_lines(columns)]
    )


def _truncate(text, width):
    text = str(text)
    if get_cwidth(text) <= width:
        return text
    remaining = max(0, width - 3)
    clipped = []
    for character in text:
        character_width = get_cwidth(character)
        if character_width > remaining:
            break
        clipped.append(character)
        remaining -= character_width
    return "".join(clipped) + "..."


def _centered(text, width):
    text = _truncate(text, width)
    return " " * max(0, (width - get_cwidth(text)) // 2) + text


def _product_version():
    try:
        return metadata.version("pico")
    except metadata.PackageNotFoundError:
        return "dev"


def _full_width_block(text):
    width = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
    fragments = [("", "\n")]
    for line in str(text).splitlines() or [""]:
        content = f" {line}"
        padding = " " * max(0, width - get_cwidth(content))
        fragments.append(("class:user", f"{content}{padding}\n"))
    return FormattedText(fragments)


def _bounded_json(value, limit=800):
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def _provider_name(model_client):
    transport = getattr(model_client, "_inner", model_client)
    metadata = getattr(transport, "provider_metadata", {})
    protocol = metadata.get("protocol_family", "") if isinstance(metadata, dict) else ""
    return _PROTOCOL_PROVIDERS.get(str(protocol), "")


class TuiRenderer:
    """Render Pico facts without becoming a second source of runtime state."""

    def __init__(self, *, no_color=False):
        self.style = _PLAIN_STYLE if no_color else _COLOR_STYLE
        self._thinking_visible = False

    def header(self, agent, *, model, columns=None):
        columns = columns or shutil.get_terminal_size((80, 24)).columns
        width = max(1, columns - 1)
        provider = _provider_name(agent.model_client)
        model_label = f"{provider}/{model}" if provider else str(model)
        compact = columns < _MEDIUM_BANNER_COLUMNS
        description = (
            "Repository-grounded coding agent" if compact else _PRODUCT_DESCRIPTION
        )
        model_summary = (
            f"Using {model_label}"
            if compact
            else f"Using {model_label} · approval {agent.approval_policy}"
        )
        shortcuts = (
            "/ commands · ctrl+c twice exit"
            if compact
            else "/ commands · esc+enter newline · ctrl+c twice exit"
        )
        print_formatted_text(
            FormattedText(
                [
                    *_logo_fragments(columns),
                    (
                        "class:meta",
                        f"\n{_centered(f'v{_product_version()}', width)}\n",
                    ),
                    ("class:meta", f"{_centered(description, width)}\n"),
                    ("class:meta", f"{_centered(model_summary, width)}\n"),
                    ("class:meta", f"{_centered(shortcuts, width)}\n"),
                ]
            ),
            style=self.style,
        )

    def toolbar(self, agent, *, model):
        mode = "sandbox" if getattr(agent, "docker_sandbox", False) else "host"
        session_id = str(agent.session.get("id", "-"))[:8]
        branch = str(getattr(agent.workspace, "branch", "-") or "-")
        workspace = str(getattr(agent.workspace, "cwd", "-"))
        provider = _provider_name(agent.model_client)
        model_label = f"{provider}/{model}" if provider else str(model)
        left = f" {workspace} ({branch}) · {session_id} · {mode}"
        right = f"{model_label} · approval {agent.approval_policy} "
        width = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
        gap = width - get_cwidth(left) - get_cwidth(right)
        if gap < 1:
            left = f" {Path(workspace).name} ({branch}) · {mode}"
            gap = max(1, width - get_cwidth(left) - get_cwidth(right))
        return FormattedText(
            [
                ("class:editor.border", f"{'─' * width}\n"),
                ("class:footer", f"{left}{' ' * gap}{right}"),
            ]
        )

    def prompt(self):
        width = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
        return FormattedText(
            [
                ("class:editor.border", f"\n{'─' * width}\n"),
                ("class:editor.prompt", " "),
            ]
        )

    def user(self, text):
        self._thinking_visible = False
        print_formatted_text(_full_width_block(text), style=self.style)

    def answer(self, text):
        self._thinking_visible = False
        print_formatted_text(
            FormattedText([("", f"\n{str(text).rstrip()}\n")]),
            style=self.style,
        )

    def approval(self, name, args):
        print_formatted_text(
            FormattedText(
                [
                    ("class:warning", "\n  ╷ APPROVAL REQUIRED\n"),
                    ("", f"  │ {name}\n"),
                    ("", f"  │ {_bounded_json(args)}\n"),
                    ("class:warning", "  ╵ default: deny\n"),
                ]
            ),
            style=self.style,
        )

    def trace(self, envelope):
        event = str(envelope.get("event", ""))
        if event == "model_requested":
            if not self._thinking_visible:
                self._thinking_visible = True
                print_formatted_text(
                    FormattedText([("class:activity", "\nThinking...\n")]),
                    style=self.style,
                )
        elif event == "tool_started":
            self._activity(str(envelope.get("name", "tool")), "running")
        elif event == "tool_finished":
            status = str(envelope.get("tool_status", "done") or "done")
            duration = envelope.get("duration_ms")
            suffix = f"{status} · {duration}ms" if isinstance(duration, int) else status
            self._activity(str(envelope.get("name", "tool")), suffix, finished=True)
        elif event == "checkpoint_created":
            checkpoint = str(envelope.get("checkpoint_id", ""))[:12]
            self._activity("checkpoint", checkpoint or "saved", finished=True)

    def notice(self, text, *, error=False):
        if error:
            self._thinking_visible = False
        style = "class:error" if error else "class:activity"
        prefix = "error: " if error else ""
        print_formatted_text(
            FormattedText([(style, f"\n{prefix}{text}\n")]),
            style=self.style,
        )

    def _activity(self, label, status, *, finished=False):
        status_style = "class:tool.success" if finished else "class:tool"
        print_formatted_text(
            FormattedText([(status_style, f" {label}  {status} \n")]),
            style=self.style,
        )
