"""Small deterministic renderers for Pony's interactive terminal surface."""

from __future__ import annotations

import json
import shutil
import sys
import textwrap
from importlib import metadata
from pathlib import Path, PurePosixPath, PureWindowsPath

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from pony.security.text import sanitize_terminal_line, sanitize_terminal_text
from pony.tools.permissions import display_permission_mode
from pony.tools.result_view import project_result_view
from pony.tui.markdown import render_markdown


_PROTOCOL_LABELS = {
    "anthropic_messages": "anthropic/messages",
    "openai_responses": "openai/responses",
    "openai_chat_completions": "openai/chat",
    "ollama_chat": "ollama/chat",
}

# Full-size Pony horse-and-wordmark welcome asset.
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

_PIXEL_GLYPHS = {
    "P": ("### ", "#  #", "### ", "#   ", "#   "),
    "O": (" ## ", "#  #", "#  #", "#  #", " ## "),
    "N": ("#  #", "## #", "####", "# ##", "#  #"),
    "Y": ("#  #", " ## ", "  # ", "  # ", "  # "),
    "C": (" ###", "#   ", "#   ", "#   ", " ###"),
    "D": ("### ", "#  #", "#  #", "#  #", "### "),
    "E": ("####", "#   ", "### ", "#   ", "####"),
}

FULL_TUI_MINIMUM_COLUMNS = 112
_PRODUCT_DESCRIPTION = "Local coding agent for repository-grounded work"
_MISSING_RESULT_VIEW = object()

_COLOR_STYLE = Style.from_dict(
    {
        "logo": "bold",
        "meta": "#858585",
        "editor.prompt": "",
        "editor.border": "#777777",
        "user": "bg:#30303d #f4f4f5",
        "user.rail": "",
        "assistant.label": "#858585",
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
        "user.rail": "",
        "assistant.label": "",
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


def _pixel_row(pattern):
    return "".join("██" if pixel == "#" else "  " for pixel in pattern)


def _wordmark_lines():
    lines = []
    for row, repeat in enumerate((2, 2, 3, 2, 2)):
        words = []
        for word in ("PONY", "CODE"):
            words.append(
                "  ".join(
                    _pixel_row(_PIXEL_GLYPHS[letter][row])
                    for letter in word
                )
            )
        lines.extend([(words[0] + "    " + words[1]).rstrip()] * repeat)
    return tuple(lines)


def _banner_lines(columns):
    width = max(FULL_TUI_MINIMUM_COLUMNS - 1, int(columns) - 1)
    horse_lines = _HORSE_LINES
    wordmark_lines = _wordmark_lines()
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
            + wordmark
        ).rstrip()
        for horse, wordmark in zip(horse_lines, wordmark_lines, strict=True)
    )


def logo_text(columns=120):
    """Return the single full-size welcome asset without a hidden variant."""
    return "\n".join(_banner_lines(columns))


def _logo_fragments(columns):
    return FormattedText(
        [("class:logo", f"{line}\n") for line in _banner_lines(columns)]
    )


def _terminal_columns():
    return shutil.get_terminal_size((80, 24)).columns


def _terminal_width(columns=None):
    columns = columns or _terminal_columns()
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


def _centered(text, width):
    text = _truncate(text, width)
    return " " * max(0, (width - get_cwidth(text)) // 2) + text


def _product_version():
    try:
        return metadata.version("pony-code")
    except metadata.PackageNotFoundError:
        return "dev"


def _bounded_json(value, limit=800):
    rendered = sanitize_terminal_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    )
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1] + "…"


def _approval_detail(label, value, width):
    return _truncate(f"  │ {label}: {value}", width) + "\n"


def _wrap_receipt_semantics(text, width):
    return textwrap.wrap(
        text,
        width=max(1, width),
        subsequent_indent="  " if width > 2 else "",
        break_long_words=True,
        break_on_hyphens=False,
    )


def _protocol_label(model_client):
    transport = getattr(model_client, "_inner", model_client)
    provider_metadata = getattr(transport, "provider_metadata", {})
    protocol = (
        provider_metadata.get("protocol_family", "")
        if isinstance(provider_metadata, dict)
        else ""
    )
    protocol = str(protocol)
    return _one_line(_PROTOCOL_LABELS.get(protocol, protocol))


def _model_label(agent, model):
    protocol = _protocol_label(getattr(agent, "model_client", None))
    transport = getattr(getattr(agent, "model_client", None), "_inner", None)
    transport = transport or getattr(agent, "model_client", None)
    safe_model = _one_line(getattr(transport, "model", model))
    return f"{protocol}/{safe_model}" if protocol else safe_model


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


def _conversation_block(label, text, width, *, label_style, content_style=""):
    rendered = render_markdown(text, width=width, base_style=content_style)
    fragments = [("", "\n"), (label_style, f"{label}\n")]
    for line in _formatted_lines(rendered):
        fragments.extend(line)
        fragments.append((content_style, "\n"))
    return FormattedText(fragments)


def _user_block(text, width):
    content_width = max(1, width - 2)
    rendered = render_markdown(text, width=content_width, base_style="class:user")
    fragments = [("", "\n")]
    for line in _formatted_lines(rendered):
        used = min(content_width, _line_width(line))
        fragments.append(("class:user.rail", "│"))
        fragments.append(("class:user", " "))
        fragments.extend(line)
        fragments.append(("class:user", " " * (width - used - 2) + "\n"))
    return FormattedText(fragments)


def _assistant_block(text, width):
    return _conversation_block(
        "Pony",
        text,
        width,
        label_style="class:assistant.label",
    )


def _one_line(value):
    return " ".join(sanitize_terminal_text(value).split())


def _quoted(value):
    return json.dumps(_one_line(value), ensure_ascii=False)


def _display_path(value):
    path = _one_line(value) or "."
    windows_path = PureWindowsPath(path)
    posix_path = PurePosixPath(path)
    if windows_path.drive or windows_path.is_absolute():
        return windows_path.name or "[path]"
    if posix_path.is_absolute():
        return posix_path.name or "[path]"
    return path


def _tool_summary(name, args):
    name = _one_line(name) or "tool"
    args = args if isinstance(args, dict) else {}
    path = _display_path(args.get("path", "."))
    if name == "list_files":
        summary = f"list {path}"
    elif name == "read_file":
        summary = f"read {path}"
    elif name == "search":
        summary = f"search {_quoted(args.get('pattern', ''))} in {path}"
    elif name == "run_shell":
        summary = "run shell command"
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
    elif name == "read_tool_result":
        summary = "read tool result"
    elif name == "repo_lookup":
        summary = f"look up {_one_line(args.get('symbol', 'symbol'))}"
    elif name == "delegate":
        summary = "delegate investigation"
    elif name == "delegate_worktrees":
        summary = "run isolated worktree agents"
    else:
        summary = name
    return summary


def _tool_activity(name, args):
    name = _one_line(name) or "tool"
    args = args if isinstance(args, dict) else {}
    path = _display_path(args.get("path", "."))
    if name in {"read_file", "memory_read"}:
        text = f"Reading {path}..."
    elif name == "read_tool_result":
        text = "Reading tool result..."
    elif name == "list_files":
        text = f"Listing {path}..."
    elif name == "memory_list":
        text = "Listing memory..."
    elif name in {"memory_search", "repo_lookup"}:
        text = "Searching..."
    elif name == "search":
        text = f"Searching {_quoted(args.get('pattern', ''))}..."
    elif name == "run_shell":
        text = "Running shell command..."
    elif name in {"write_file", "memory_save"}:
        text = f"Writing {path}..." if name == "write_file" else "Writing memory..."
    elif name == "patch_file":
        text = f"Patching {path}..."
    elif name in {"delegate", "delegate_worktrees"}:
        text = "Delegating..."
    else:
        text = f"Running {name}..."
    return text


def _result_view_detail(value):
    view = project_result_view(value)
    if view is None:
        return "result details unavailable", True
    delivery = view["delivery"]
    if delivery == "inline":
        return "", False
    if delivery == "page":
        detail = (
            f"lines {view['start_line']}-{view['end_line']}/"
            f"{view['total_lines']}"
        )
        if "next_start" in view:
            detail += f" · more from {view['next_start']}"
        elif "next_start_byte" in view:
            detail += f" · more at byte {view['next_start_byte']}"
    else:
        detail = "output truncated"
        if "exit_code" in view:
            detail = f"exit {view['exit_code']} · {detail}"
    reasons = view.get("reasons") or []
    if reasons:
        detail += f" · limit={'+'.join(reasons)}"
    if delivery == "preview":
        detail += (
            " · recoverable until this turn ends"
            if view["recoverable"]
            else " · not recoverable"
        )
    return detail, delivery == "preview"


class TuiRenderer:
    """Project durable runtime facts into a quiet terminal conversation."""

    def __init__(self, *, no_color=False):
        self.style = _PLAIN_STYLE if no_color else _COLOR_STYLE
        self._activity_visible = False
        self._activity_width = 0
        self._activity_text = ""
        self._activity_source = ""
        self._active_tool = ""
        self._active_tool_activity = ""

    def _write(self, value, **kwargs):
        print_formatted_text(value, style=self.style, **kwargs)

    def welcome(self, agent, *, model, columns=None):
        columns = columns or _terminal_columns()
        width = max(1, columns - 1)
        current_mode = getattr(agent, "current_permission_mode", None)
        permission_mode = display_permission_mode(
            current_mode()
            if callable(current_mode)
            else getattr(agent, "session", {}).get("permission_mode", "auto")
        )
        target = _model_label(agent, model)
        return FormattedText(
            [
                *_logo_fragments(columns),
                ("class:meta", f"\n{_centered(f'v{_product_version()}', width)}\n"),
                ("class:meta", f"{_centered(_PRODUCT_DESCRIPTION, width)}\n"),
                (
                    "class:meta",
                    f"{_centered(f'Ready · {target} · permission {permission_mode}', width)}\n",
                ),
            ]
        )

    def header(self, agent, *, model, columns=None):
        self._write(self.welcome(agent, model=model, columns=columns))

    def toolbar(self, agent, *, model, columns=None):
        width = _terminal_width(columns)
        branch = _one_line(getattr(agent.workspace, "branch", "-") or "-")
        workspace = _one_line(getattr(agent.workspace, "cwd", "-"))
        repository = Path(workspace).name or "-"
        left = f" {repository} ({branch})"
        current_mode = getattr(agent, "current_permission_mode", None)
        permission_mode = _one_line(
            current_mode()
            if callable(current_mode)
            else getattr(agent, "session", {}).get("permission_mode", "auto")
        ) or "auto"
        permission_mode = display_permission_mode(permission_mode)
        right = f"{permission_mode} · {_model_label(agent, model)} "
        candidates = (
            f"{left} | {right}",
            f" {repository} | {right}",
            right,
        )
        footer = next(
            (candidate for candidate in candidates if get_cwidth(candidate) <= width),
            _truncate(right, width),
        )
        return FormattedText(
            [
                ("class:editor.border", f"{'─' * width}\n"),
                ("class:footer", footer),
            ]
        )

    def prompt(self, *, columns=None, pending=0):
        columns = columns or _terminal_columns()
        width = _terminal_width(columns)
        header = []
        if columns < FULL_TUI_MINIMUM_COLUMNS:
            header.append("Widen terminal")
        if pending:
            header.append(f"Queued {pending}/5")
        fragments = [("class:editor.border", f"\n{'─' * width}\n")]
        if header:
            style = (
                "class:warning"
                if columns < FULL_TUI_MINIMUM_COLUMNS
                else "class:meta"
            )
            fragments.append((style, f"{_truncate(' · '.join(header), width)}\n"))
        fragments.append(("class:editor.prompt", "› "))
        return FormattedText(fragments)

    def resume_card(self, projection):
        goal = projection["goal"]
        lines = [
            "Resume",
            "permission [session]: "
            f"{display_permission_mode(projection['permission_mode'])}",
        ]
        if goal["text"]:
            lines.append(f"goal [{goal['source']}]: {goal['text']}")
        checkpoint = projection["checkpoint"]
        if checkpoint["status"] or checkpoint["blocker"]:
            lines.append(
                "checkpoint [checkpoint]: "
                f"status={checkpoint['status'] or '-'}; "
                f"blocker={checkpoint['blocker'] or '-'}"
            )
        lines.extend(
            f"next [checkpoint]: {next_step}"
            for next_step in checkpoint["next_steps"]
        )
        lines.append(f"resume [resume_state]: {projection['resume']['status'] or '-'}")
        model = projection["model"]
        if model["protocol_family"] or model["model"]:
            label = "/".join(
                value
                for value in (model["protocol_family"], model["model"])
                if value
            )
            lines.append(f"model [provider_binding]: {label}")
        safe_text = sanitize_terminal_text("\n".join(lines)).strip()
        return FormattedText([("class:activity", f"\n{safe_text}\n")])

    def resume(self, projection):
        self._clear_activity()
        self._write(self.resume_card(projection))

    def user(self, text, *, columns=None, restore_activity=False):
        activity = self._activity_source if restore_activity else ""
        self._clear_activity()
        self._write(_user_block(text, _terminal_width(columns)))
        if activity:
            self._show_activity(activity)

    def turn_started(self, text, *, columns=None):
        self.user(text, columns=columns)

    def answer(self, text, *, columns=None):
        self._clear_activity()
        width = _terminal_width(columns)
        self._write(_assistant_block(text, width))

    def stream_committed(self):
        self._show_activity("Receiving...")

    def stream_preview(self, snapshot, *, columns=None):
        safe_text = sanitize_terminal_line(snapshot)
        if not safe_text:
            return False
        prefix = "Pony - "
        width = _terminal_width(columns)
        available = width - get_cwidth(prefix)
        if available <= 3:
            return False
        projected = _truncate(safe_text, available)
        if get_cwidth(safe_text) > available and projected == "...":
            return False
        self._show_activity(prefix + safe_text, columns=columns)
        return True

    def stream_finished(self):
        self._clear_activity()

    def approval(self, name, args, *, columns=None):
        self._clear_activity()
        width = _terminal_width(columns)
        safe_name = _one_line(name)
        if safe_name == "exit_plan_mode" and isinstance(args, dict):
            plan = str(args.get("plan", ""))
            revision = int(args.get("revision", 0) or 0)
            self._write(
                FormattedText(
                    [
                        (
                            "class:warning",
                            "\n" + _truncate("  ╷ PLAN APPROVAL REQUIRED", width) + "\n",
                        ),
                        ("", _truncate(f"  │ revision {revision}", width) + "\n"),
                    ]
                )
            )
            self._write(render_markdown(plan, width=width))
            self._write(
                FormattedText(
                    [
                        (
                            "class:warning",
                            "\n" + _truncate("  ╵ default: deny", width) + "\n",
                        )
                    ]
                )
            )
            return
        self._write(
            FormattedText(
                [
                    (
                        "class:warning",
                        "\n" + _truncate("  ╷ APPROVAL REQUIRED", width) + "\n",
                    ),
                    (
                        "",
                        _approval_detail(
                            "action",
                            _tool_summary(safe_name, args),
                            width,
                        ),
                    ),
                    ("", _approval_detail("details", _bounded_json(args), width)),
                    (
                        "class:warning",
                        _truncate("  ╵ default: deny", width) + "\n",
                    ),
                ]
            )
        )

    def approval_resolved(self, accepted):
        self._clear_activity()
        if accepted and self._active_tool_activity:
            self._show_activity(self._active_tool_activity)

    def trace(self, envelope):
        event = str(envelope.get("event", ""))
        if event == "model_requested":
            self._show_activity("Working...")
        elif event == "tool_started":
            name = envelope.get("name", "tool")
            args = envelope.get("args", {})
            self._active_tool = _tool_summary(name, args)
            self._active_tool_activity = _tool_activity(name, args)
            self._show_activity(self._active_tool_activity)
        elif event == "tool_executed":
            self._clear_activity()
            status = str(envelope.get("tool_status", ""))
            self._tool_receipt(
                status,
                envelope.get("tool_error_code", ""),
                envelope.get("result_view", _MISSING_RESULT_VIEW),
            )
            self._active_tool = ""
            self._active_tool_activity = ""
        elif event in {"context_compacted", "context_recovery"}:
            self._clear_activity()
        elif event == "model_failed":
            self._clear_activity()
        elif event == "finalization_failed":
            self._clear_activity()
        elif event == "tool_interrupted":
            self._tool_receipt("interrupted", envelope.get("tool_error_code", ""))
            self._active_tool = ""
            self._active_tool_activity = ""
        elif event == "run_finished":
            self._clear_activity()

    def notice(self, text, *, error=False, restore_activity=False):
        activity = self._activity_source if restore_activity else ""
        self._clear_activity()
        style = "class:error" if error else "class:activity"
        prefix = "error: " if error else ""
        safe_text = sanitize_terminal_text(text).strip()
        self._write(FormattedText([(style, f"\n{prefix}{safe_text}\n")]))
        if activity:
            self._show_activity(activity)

    def close(self):
        self._clear_activity(newline=True)

    def resize(self, columns):
        if not self._activity_visible:
            return
        source = self._activity_source
        projected = _truncate(source, _terminal_width(columns))
        if projected == self._activity_text:
            return
        self._clear_activity(columns=columns)
        self._show_activity(source, columns=columns)

    def _show_activity(self, text, *, columns=None):
        source = str(text)
        projected = _truncate(source, _terminal_width(columns))
        if self._activity_visible and self._activity_text == projected:
            self._activity_source = source
            return
        self._clear_activity()
        self._activity_visible = True
        self._activity_width = get_cwidth(projected)
        self._activity_text = projected
        self._activity_source = source
        self._write(
            FormattedText([("class:activity", projected)]),
            end="",
            flush=True,
        )

    def _clear_activity(self, *, newline=False, columns=None):
        if not self._activity_visible:
            return
        suffix = "\n" if newline else ""
        clear_width = min(self._activity_width, _terminal_width(columns))
        sys.stdout.write(f"\r{' ' * clear_width}\r{suffix}")
        sys.stdout.flush()
        self._activity_visible = False
        self._activity_width = 0
        self._activity_text = ""
        self._activity_source = ""

    def _tool_receipt(self, status, error_code, result_view=_MISSING_RESULT_VIEW):
        self._clear_activity()
        width = _terminal_width()
        summary = self._active_tool or "tool"
        detail = ""
        result_warning = False
        if result_view is not _MISSING_RESULT_VIEW:
            detail, result_warning = _result_view_detail(result_view)
        marker = (
            "!"
            if status in {"ok", "partial_success"} and result_warning
            else "✓"
            if status == "ok"
            else "!"
            if status == "partial_success"
            else "×"
        )
        style = "class:tool" if status == "ok" else "class:tool.error"
        prefix = marker
        if status != "ok":
            prefix += f" {(_one_line(status) or 'error')}"
            safe_code = _one_line(error_code)
            if safe_code:
                prefix += f" · code={safe_code}"
        separator = " " if status == "ok" else " · "
        available = width - get_cwidth(prefix) - get_cwidth(separator)
        receipt = prefix
        if summary and available > 3:
            receipt += separator + _truncate(summary, available)
        if not detail:
            if get_cwidth(receipt) <= width:
                self._write(FormattedText([(style, f"{receipt}\n")]))
                return
            lines = _wrap_receipt_semantics(prefix, width)
            if summary:
                indent = "  " if width > 2 else ""
                summary_width = max(1, width - get_cwidth(indent))
                lines.append(indent + _truncate(summary, summary_width))
            self._write(FormattedText([(style, f"{line}\n") for line in lines]))
            return
        combined = f"{receipt} · {detail}"
        if get_cwidth(combined) <= width:
            self._write(FormattedText([(style, f"{combined}\n")]))
            return
        indent = "  " if width > 2 else ""
        detail_width = max(1, width - get_cwidth(indent))
        lines = (
            [receipt]
            if get_cwidth(receipt) <= width
            else _wrap_receipt_semantics(prefix, width)
        )
        if receipt != prefix and get_cwidth(receipt) > width:
            lines.append(indent + _truncate(summary, detail_width))
        self._write(
            FormattedText(
                [(style, f"{line}\n") for line in lines]
                + [(style, indent + _truncate(detail, detail_width) + "\n")]
            )
        )
