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

from pony.tools.permissions import display_permission_mode
from pony.tui.markdown import render_markdown, sanitize_terminal_text


_PROTOCOL_LABELS = {
    "anthropic_messages": "anthropic/messages",
    "openai_responses": "openai/responses",
    "openai_chat_completions": "openai/chat",
    "ollama_chat": "ollama/chat",
}

_FAILURE_STATUSES = frozenset({"error", "partial_success", "rejected"})

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
COMPACT_TUI_MINIMUM_COLUMNS = 80
_PRODUCT_DESCRIPTION = "Local coding agent for repository-grounded work"

_COLOR_STYLE = Style.from_dict(
    {
        "logo": "bold",
        "meta": "#858585",
        "editor.prompt": "",
        "editor.border": "#777777",
        "user": "#d7d7d7",
        "user.label": "bold #f4f4f5",
        "assistant.label": "bold #f4f4f5",
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
        "user.label": "bold",
        "assistant.label": "bold",
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


def _clip_cells(text, width):
    text = str(text)
    remaining = max(0, int(width))
    clipped = []
    for character in text:
        character_width = get_cwidth(character)
        if character_width > remaining:
            break
        clipped.append(character)
        remaining -= character_width
    return "".join(clipped)


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
    prefix = f"  │ {label}: "
    available = max(0, width - get_cwidth(prefix))
    return prefix + _truncate(value, available) + "\n"


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
    content_width = max(1, width - 2)
    rendered = render_markdown(text, width=content_width, base_style=content_style)
    fragments = [("", "\n"), (label_style, f"{label}\n")]
    for line in _formatted_lines(rendered):
        fragments.append((content_style, "  "))
        fragments.extend(line)
        fragments.append((content_style, "\n"))
    return FormattedText(fragments)


def _user_block(text, width):
    return _conversation_block(
        "YOU",
        text,
        width,
        label_style="class:user.label",
        content_style="class:user",
    )


def _assistant_block(text, width):
    return _conversation_block(
        "PONY",
        text,
        width,
        label_style="class:assistant.label",
    )


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
    elif name == "delegate_worktrees":
        summary = "run isolated worktree agents"
    else:
        summary = name
    return _truncate(summary, max(1, width - 2))


class TuiRenderer:
    """Project durable runtime facts into a quiet terminal conversation."""

    def __init__(self, *, no_color=False):
        self.style = _PLAIN_STYLE if no_color else _COLOR_STYLE
        self._status = "Ready"
        self._context_status = ""
        self._activity_visible = False
        self._activity_width = 0
        self._activity_text = ""
        self._active_tool = ""

    def _write(self, value, **kwargs):
        print_formatted_text(value, style=self.style, **kwargs)

    def welcome(self, agent, *, model, columns=None):
        columns = columns or shutil.get_terminal_size((80, 24)).columns
        width = max(1, columns - 1)
        compact = columns < FULL_TUI_MINIMUM_COLUMNS
        current_mode = getattr(agent, "current_permission_mode", None)
        permission_mode = display_permission_mode(
            current_mode()
            if callable(current_mode)
            else getattr(agent, "session", {}).get("permission_mode", "auto")
        )
        target = _model_label(agent, model)
        if compact:
            version_line = _truncate(
                f"v{_product_version()} · {_PRODUCT_DESCRIPTION}",
                width,
            )
            ready_line = _truncate(
                f"Ready · {target} · permission {permission_mode}",
                width,
            )
            return FormattedText(
                [
                    ("class:logo", "PONY CODE\n"),
                    ("class:meta", f"{version_line}\n"),
                    ("class:key", ready_line[: len("Ready")]),
                    ("class:meta", f"{ready_line[len('Ready') :]}\n"),
                ]
            )
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

    def toolbar(self, agent, *, model, columns=None, busy=False, pending=0):
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
        self._record_context(getattr(agent, "last_request_metadata", {}))
        state = self._status
        if pending:
            state += f" · queue {pending}/5"
        elif busy and state == "Ready":
            state = "Working"
        if self._context_status:
            state += f" · {self._context_status}"
        right = f"{permission_mode} · {_model_label(agent, model)} "
        middle = f"{state}"
        candidates = (
            f"{left} · {middle} | {right}",
            f" {repository} · {middle} | {right}",
            f"{middle} | {right}",
            f"{self._status} | {right}",
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

    def prompt(self, *, columns=None):
        width = _terminal_width(columns)
        return FormattedText(
            [
                ("class:editor.border", f"\n{'─' * width}\n"),
                ("class:editor.prompt", "Message Pony\n› "),
            ]
        )

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

    def user(self, text, *, columns=None):
        self._clear_activity()
        self._write(_user_block(text, _terminal_width(columns)))

    def turn_started(self, text, *, columns=None):
        self.user(text, columns=columns)
        self._status = "Preparing"
        self._show_activity("PONY  Preparing context...")

    def answer(self, text, *, columns=None):
        self._clear_activity()
        width = _terminal_width(columns)
        self._write(_assistant_block(text, width))

    def approval(self, name, args, *, columns=None):
        self._clear_activity()
        self._status = "Waiting for approval"
        width = _terminal_width(columns)
        safe_name = _one_line(name)
        if safe_name == "exit_plan_mode" and isinstance(args, dict):
            plan = str(args.get("plan", ""))
            revision = int(args.get("revision", 0) or 0)
            self._write(
                FormattedText(
                    [
                        ("class:warning", "\n  ╷ PLAN APPROVAL REQUIRED\n"),
                        ("", f"  │ revision {revision}\n"),
                    ]
                )
            )
            self._write(render_markdown(plan, width=width))
            self._write(
                FormattedText([("class:warning", "\n  ╵ default: deny\n")])
            )
            return
        self._write(
            FormattedText(
                [
                    ("class:warning", "\n  ╷ APPROVAL REQUIRED\n"),
                    (
                        "",
                        _approval_detail(
                            "action",
                            _tool_summary(safe_name, args, width),
                            width,
                        ),
                    ),
                    ("", _approval_detail("details", _bounded_json(args), width)),
                    ("class:warning", "  ╵ default: deny\n"),
                ]
            )
        )

    def approval_resolved(self, accepted):
        self._clear_activity()
        self._status = "Using tool" if accepted else "Approval denied"
        if accepted and self._active_tool:
            self._show_activity(f"PONY  Using tool · {self._active_tool}...")

    def trace(self, envelope):
        event = str(envelope.get("event", ""))
        request_metadata = envelope.get("request_metadata", {})
        if isinstance(request_metadata, dict):
            self._record_context(request_metadata)
        if event == "run_started":
            self._status = "Preparing"
        elif event == "prompt_built":
            self._status = "Waiting for model"
            self._show_activity("PONY  Waiting for model...")
        elif event == "model_requested":
            origin = str(envelope.get("attempt_origin", ""))
            if origin in {"model_retry", "retry_action"}:
                self._status = "Retrying"
                self._show_activity("PONY  Retrying model request...")
            elif origin in {"compaction", "split_compaction"}:
                self._status = "Compacting"
                self._show_activity("PONY  Compacting context...")
            else:
                self._status = "Waiting for model"
                self._show_activity("PONY  Waiting for model...")
        elif event == "tool_started":
            width = _terminal_width()
            activity_prefix = "PONY  Using tool · "
            activity_suffix = "..."
            summary_width = max(
                0,
                width
                - get_cwidth(activity_prefix)
                - get_cwidth(activity_suffix),
            )
            summary = _clip_cells(
                _tool_summary(
                    envelope.get("name", "tool"),
                    envelope.get("args", {}),
                    width,
                ),
                summary_width,
            )
            self._active_tool = summary
            self._status = "Using tool"
            self._show_activity(
                f"{activity_prefix}{summary}{activity_suffix}"
            )
        elif event == "tool_executed":
            self._clear_activity()
            status = str(envelope.get("tool_status", ""))
            if status in _FAILURE_STATUSES:
                self._tool_failure(status, envelope.get("result", ""))
            elif self._active_tool:
                self._write(FormattedText([("class:tool", f"  ✓ {self._active_tool}\n")]))
            self._active_tool = ""
        elif event in {"context_compacted", "context_recovery"}:
            self._clear_activity()
            self._status = "Compacted"
            self._write(FormattedText([("class:activity", "PONY  Context compacted\n")]))
        elif event == "model_failed":
            self._clear_activity()
            self._status = (
                "Interrupted"
                if envelope.get("outcome") == "interrupted"
                else "Failed"
            )
        elif event == "finalization_failed":
            self._clear_activity()
            self._status = "Failed"
        elif event == "tool_interrupted":
            self._tool_failure("interrupted", "")
            self._active_tool = ""
        elif event == "run_finished":
            self._clear_activity()
            status = str(envelope.get("status", ""))
            stop_reason = str(envelope.get("stop_reason", ""))
            if stop_reason == "interrupted":
                self._status = "Interrupted"
            elif status == "completed" and stop_reason == "final_answer_returned":
                self._status = "Completed"
            else:
                self._status = "Failed"

    def notice(self, text, *, error=False):
        self._clear_activity()
        style = "class:error" if error else "class:activity"
        prefix = "error: " if error else ""
        safe_text = sanitize_terminal_text(text).strip()
        self._write(FormattedText([(style, f"\n{prefix}{safe_text}\n")]))

    def close(self):
        self._clear_activity(newline=True)

    def _show_activity(self, text):
        if self._activity_visible and self._activity_text == text:
            return
        self._clear_activity()
        self._activity_visible = True
        self._activity_width = get_cwidth(text)
        self._activity_text = text
        self._write(
            FormattedText([("class:activity", text)]),
            end="",
            flush=True,
        )

    def _clear_activity(self, *, newline=False):
        if not self._activity_visible:
            return
        suffix = "\n" if newline else ""
        clear_width = min(self._activity_width, _terminal_width())
        sys.stdout.write(f"\r{' ' * clear_width}\r{suffix}")
        sys.stdout.flush()
        self._activity_visible = False
        self._activity_width = 0
        self._activity_text = ""

    def _record_context(self, request_metadata):
        if not isinstance(request_metadata, dict):
            return
        breakdown = request_metadata.get("context_breakdown", {})
        budget = breakdown.get("budget", {}) if isinstance(breakdown, dict) else {}
        used = budget.get("used") if isinstance(budget, dict) else None
        limit = budget.get("input_limit") if isinstance(budget, dict) else None
        if (
            type(used) is int
            and type(limit) is int
            and used >= 0
            and limit > 0
        ):
            percent = min(100, round(used * 100 / limit))
            self._context_status = f"ctx {percent}%"

    def _tool_failure(self, status, result):
        self._clear_activity()
        width = _terminal_width()
        detail = _one_line(result)
        message = f"{status}: {detail}" if detail else status
        self._write(
            FormattedText(
                [("class:tool.error", f"  ↳ {_truncate(message, width - 4)}\n")]
            )
        )
