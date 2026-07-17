"""Small deterministic renderers for Pico's interactive terminal surface."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth


# Terminal-scale adaptation of the horse silhouette selected for Pico's TUI.
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

_PROTOCOL_PROVIDERS = {
    "anthropic_messages": "anthropic",
    "openai_responses": "openai",
    "openai_chat_completions": "openai",
    "ollama_chat": "ollama",
}

_COLOR_STYLE = Style.from_dict(
    {
        "logo": "bold",
        "logo.accent": "bold",
        "logo.name": "bold",
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
        "logo.accent": "bold",
        "logo.name": "bold",
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


def logo_text():
    """Return the color-independent terminal logo used by tests and fallbacks."""
    lines = list(_HORSE_LINES)
    lines[5] += "  HERMES"
    return "\n".join(lines)


def _logo_fragments():
    fragments = []
    for index, line in enumerate(_HORSE_LINES):
        fragments.append(("class:logo.accent", line))
        if index == 5:
            fragments.append(("class:logo.name", "  HERMES"))
        fragments.append(("", "\n"))
    return FormattedText(fragments)


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

    def header(self, agent, *, model):
        print_formatted_text(
            FormattedText(
                [
                    *_logo_fragments(),
                    ("class:key", "/"),
                    ("class:meta", " commands  "),
                    ("class:key", "esc+enter"),
                    ("class:meta", " newline  "),
                    ("class:key", "ctrl+c twice"),
                    ("class:meta", " exit\n"),
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
