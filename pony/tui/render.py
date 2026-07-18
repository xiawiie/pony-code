"""Small deterministic renderers for Pony's interactive terminal surface."""

from __future__ import annotations

import json
import shutil
import sys
from importlib import metadata
from pathlib import Path

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from pony.tui.markdown import render_markdown, sanitize_terminal_text


_PROTOCOL_PROVIDERS = {
    "anthropic_messages": "anthropic",
    "openai_responses": "openai",
    "openai_chat_completions": "openai",
    "ollama_chat": "ollama",
}

_FAILURE_STATUSES = frozenset({"error", "partial_success", "rejected"})

_COLOR_STYLE = Style.from_dict(
    {
        "logo": "bold",
        "meta": "#858585",
        "editor.prompt": "",
        "editor.border": "#777777",
        "user": "bg:#30303d #f4f4f5",
        "activity": "italic #858585",
        "tool": "#bdbdbd",
        "tool.error": "#ff4d4f",
        "warning": "bold #d29922",
        "error": "bold #ff4d4f",
        "key": "bold",
        "footer": "#777777",
        "markdown.code": "bg:#2b2b2b #f0f0f0",
        "markdown.quote": "#858585",
        "markdown.link": "underline",
        "markdown.rule": "#777777",
        "markdown.table": "#d7d7d7",
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
        "meta": "",
        "editor.prompt": "bold",
        "editor.border": "",
        "user": "",
        "activity": "italic",
        "tool": "",
        "tool.error": "bold",
        "warning": "bold",
        "error": "bold",
        "key": "bold",
        "footer": "",
        "markdown.code": "underline",
        "markdown.quote": "",
        "markdown.link": "underline",
        "markdown.rule": "",
        "markdown.table": "",
        "bottom-toolbar": "noreverse",
        "bottom-toolbar.text": "",
    }
)


def _terminal_width(columns=None):
    columns = columns or shutil.get_terminal_size((80, 24)).columns
    return max(1, int(columns) - 1)


def _truncate(text, width):
    text = str(text)
    width = max(0, int(width))
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


def _product_version():
    try:
        return metadata.version("pony-code")
    except metadata.PackageNotFoundError:
        return "dev"


def _bounded_json(value, limit=800):
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1] + "…"


def _provider_name(model_client):
    transport = getattr(model_client, "_inner", model_client)
    provider_metadata = getattr(transport, "provider_metadata", {})
    protocol = (
        provider_metadata.get("protocol_family", "")
        if isinstance(provider_metadata, dict)
        else ""
    )
    return _PROTOCOL_PROVIDERS.get(str(protocol), "")


def _model_label(agent, model):
    provider = _provider_name(getattr(agent, "model_client", None))
    safe_model = _one_line(model)
    return f"{provider}/{safe_model}" if provider else safe_model


def _formatted_lines(value):
    lines = [[]]
    for style, text in value:
        parts = text.split("\n")
        for index, part in enumerate(parts):
            if part:
                lines[-1].append((style, part))
            if index < len(parts) - 1:
                lines.append([])
    if len(lines) > 1 and not lines[-1]:
        lines.pop()
    return lines


def _line_width(line):
    return sum(get_cwidth(text) for _style, text in line)


def _user_block(text, width):
    content_width = max(1, width - 2)
    rendered = render_markdown(text, width=content_width, base_style="class:user")
    fragments = [("", "\n"), ("class:user", " " * width + "\n")]
    for line in _formatted_lines(rendered):
        used = min(content_width, _line_width(line))
        fragments.append(("class:user", " "))
        fragments.extend(line)
        fragments.append(("class:user", " " * (width - used - 1) + "\n"))
    fragments.append(("class:user", " " * width + "\n"))
    return FormattedText(fragments)


def _one_line(value):
    return " ".join(sanitize_terminal_text(value).split())


def _quoted(value):
    return json.dumps(_one_line(value), ensure_ascii=False)


def _tool_summary(name, args, width):
    name = _one_line(name) or "tool"
    args = args if isinstance(args, dict) else {}
    path = _one_line(args.get("path", ".")) or "."
    if name == "list_files":
        summary = f"list {path}"
    elif name == "read_file":
        summary = f"read {path}"
    elif name == "search":
        summary = f"search {_quoted(args.get('pattern', ''))} in {path}"
    elif name == "run_shell":
        summary = f"$ {_one_line(args.get('command', ''))}"
    elif name == "write_file":
        summary = f"write {path}"
    elif name == "patch_file":
        summary = f"patch {path}"
    elif name == "memory_list":
        summary = f"list memory {_one_line(args.get('prefix', ''))}".rstrip()
    elif name == "memory_read":
        summary = f"read memory {path}"
    elif name == "memory_search":
        summary = f"search memory {_quoted(args.get('query', ''))}"
    elif name == "memory_save":
        scope = _one_line(args.get("scope", "workspace")) or "workspace"
        summary = f"save {scope} memory"
    elif name == "repo_lookup":
        summary = f"look up {_one_line(args.get('symbol', 'symbol'))}"
    elif name == "delegate":
        summary = "delegate investigation"
    else:
        summary = name
    return _truncate(summary, max(1, width - 2))


class TuiRenderer:
    """Project durable runtime facts into a quiet terminal conversation."""

    def __init__(self, *, no_color=False):
        self.style = _PLAIN_STYLE if no_color else _COLOR_STYLE
        self._working_visible = False
        self._working_width = 0

    def _write(self, value, **kwargs):
        print_formatted_text(value, style=self.style, **kwargs)

    def header(self, agent, *, model, columns=None):
        del agent, model, columns
        self._write(
            FormattedText([("class:logo", f"PONY CODE · v{_product_version()}\n")])
        )

    def toolbar(self, agent, *, model, columns=None):
        width = _terminal_width(columns)
        mode = "sandbox" if getattr(agent, "docker_sandbox", False) else "host"
        branch = _one_line(getattr(agent.workspace, "branch", "-") or "-")
        workspace = _one_line(getattr(agent.workspace, "cwd", "-"))
        repository = Path(workspace).name or "-"
        left = f" {mode} · {repository} ({branch})"
        approval = _one_line(getattr(agent, "approval_policy", "")) or "-"
        right = f"approval {approval} · {_model_label(agent, model)} "
        gap = width - get_cwidth(left) - get_cwidth(right)
        if gap < 1:
            right = _truncate(right, max(1, min(get_cwidth(right), width * 2 // 3)))
            left = _truncate(left, max(1, width - get_cwidth(right) - 1))
            gap = max(1, width - get_cwidth(left) - get_cwidth(right))
        return FormattedText(
            [
                ("class:editor.border", f"{'─' * width}\n"),
                ("class:footer", f"{left}{' ' * gap}{right}"),
            ]
        )

    def prompt(self, *, columns=None):
        width = _terminal_width(columns)
        return FormattedText(
            [
                ("class:editor.border", f"\n{'─' * width}\n"),
                ("class:editor.prompt", " "),
            ]
        )

    def user(self, text, *, columns=None):
        self._clear_working()
        self._write(_user_block(text, _terminal_width(columns)))

    def answer(self, text, *, columns=None):
        self._clear_working()
        width = _terminal_width(columns)
        markdown = render_markdown(text, width=width)
        self._write(FormattedText([("", "\n"), *markdown]))

    def approval(self, name, args):
        self._clear_working()
        safe_name = _one_line(name)
        self._write(
            FormattedText(
                [
                    ("class:warning", "\n  ╷ APPROVAL REQUIRED\n"),
                    ("", f"  │ {safe_name}\n"),
                    ("", f"  │ {_bounded_json(args)}\n"),
                    ("class:warning", "  ╵ default: deny\n"),
                ]
            )
        )

    def trace(self, envelope):
        event = str(envelope.get("event", ""))
        if event == "model_requested":
            self._show_working()
        elif event == "tool_started":
            self._clear_working()
            width = _terminal_width()
            summary = _tool_summary(
                envelope.get("name", "tool"),
                envelope.get("args", {}),
                width,
            )
            self._write(FormattedText([("class:tool", f"› {summary}\n")]))
        elif event == "tool_executed":
            status = str(envelope.get("tool_status", ""))
            if status in _FAILURE_STATUSES:
                self._tool_failure(status, envelope.get("result", ""))
        elif event == "tool_interrupted":
            self._tool_failure("interrupted", "")

    def notice(self, text, *, error=False):
        self._clear_working()
        style = "class:error" if error else "class:activity"
        prefix = "error: " if error else ""
        safe_text = sanitize_terminal_text(text).strip()
        self._write(FormattedText([(style, f"\n{prefix}{safe_text}\n")]))

    def close(self):
        self._clear_working(newline=True)

    def _show_working(self):
        if self._working_visible:
            return
        text = "Working…"
        self._working_visible = True
        self._working_width = get_cwidth(text)
        self._write(
            FormattedText([("class:activity", text)]),
            end="",
            flush=True,
        )

    def _clear_working(self, *, newline=False):
        if not self._working_visible:
            return
        suffix = "\n" if newline else ""
        sys.stdout.write(f"\r{' ' * self._working_width}\r{suffix}")
        sys.stdout.flush()
        self._working_visible = False
        self._working_width = 0

    def _tool_failure(self, status, result):
        width = _terminal_width()
        detail = _one_line(result)
        message = f"{status}: {detail}" if detail else status
        self._write(
            FormattedText(
                [("class:tool.error", f"  ↳ {_truncate(message, width - 4)}\n")]
            )
        )
