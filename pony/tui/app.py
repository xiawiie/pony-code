"""Prompt-toolkit shell around Pony's existing REPL semantics."""

from __future__ import annotations

import os
import shutil
import sys
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension
from prompt_toolkit.shortcuts import CompleteStyle

from pony.cli.help import SLASH_COMMANDS
from pony.runtime.resume import active_prompt_history
from pony.tui.render import TuiRenderer


_MINIMUM_COLUMNS = 40
_DOUBLE_INTERRUPT_SECONDS = 1.5
_MAX_EDITOR_LINES = 6
_COMPLETION_ROWS = 5


class _CompactPromptSession(PromptSession):
    """Keep the multiline editor inline with native terminal scrollback."""

    def _get_default_buffer_control_height(self):
        if self.default_buffer.complete_state is not None:
            return Dimension(
                min=self.reserve_space_for_menu,
                max=self.reserve_space_for_menu,
            )
        lines = min(self.default_buffer.document.line_count, _MAX_EDITOR_LINES)
        return Dimension(min=lines, max=lines)


def should_use_tui(*, stdin=None, stdout=None, environ=None, columns=None):
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    environ = os.environ if environ is None else environ
    if not getattr(stdin, "isatty", lambda: False)():
        return False
    if not getattr(stdout, "isatty", lambda: False)():
        return False
    if environ.get("TERM", "").casefold() == "dumb":
        return False
    width = columns
    if width is None:
        width = shutil.get_terminal_size((80, 24)).columns
    return width >= _MINIMUM_COLUMNS


class SlashCommandCompleter(Completer):
    """Complete documented local commands only at the start of a prompt."""

    def get_completions(self, document, _complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or any(character.isspace() for character in text):
            return
        for command in SLASH_COMMANDS:
            if command.name.startswith(text):
                yield Completion(
                    command.name,
                    start_position=-len(text),
                    display=command.usage,
                    display_meta=command.summary,
                )


def _key_bindings():
    bindings = KeyBindings()

    @bindings.add("/")
    def open_command_menu(event):
        buffer = event.current_buffer
        buffer.insert_text("/")
        if buffer.document.text_before_cursor == "/":
            buffer.start_completion(select_first=False)

    @bindings.add("enter")
    def submit(event):
        buffer = event.current_buffer
        if buffer.document.text_before_cursor.endswith("\\"):
            buffer.delete_before_cursor(1)
            buffer.insert_text("\n")
            return
        buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def insert_newline(event):
        event.current_buffer.insert_text("\n")

    return bindings


def _continuation(width, _line_number, _is_soft_wrap):
    return FormattedText([("class:editor.prompt", " " * width)])


def _history(items):
    history = InMemoryHistory()
    for item in items:
        history.append_string(item)
    return history


def run_tui(
    agent,
    *,
    model,
    no_color,
    handle_input,
    show_header=True,
    resume_projection=None,
    prompt_history=(),
):
    """Run one synchronous Pony turn at a time in an inline terminal UI."""
    renderer = TuiRenderer(
        no_color=no_color or os.environ.get("NO_COLOR") is not None,
    )
    if show_header:
        renderer.header(agent, model=model)
    if resume_projection is not None:
        renderer.resume(resume_projection)
    session = _CompactPromptSession(
        history=_history(prompt_history),
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        erase_when_done=True,
        enable_history_search=True,
        key_bindings=_key_bindings(),
        multiline=True,
        reserve_space_for_menu=_COMPLETION_ROWS,
        style=renderer.style,
    )

    def confirm(message):
        answer = session.prompt(
            FormattedText([("class:warning", message)]),
            multiline=False,
            bottom_toolbar=None,
        )
        return answer.strip().casefold() in {"y", "yes"}

    def approve(name, args):
        renderer.approval(name, args)
        return confirm("  Approve once? [y/N] ")

    previous_listener = getattr(agent, "_trace_listener", None)
    previous_approval_prompt = getattr(agent, "_approval_prompt", None)
    agent._trace_listener = renderer.trace
    agent._approval_prompt = approve
    last_interrupt = 0.0

    def process(user_input):
        def refresh_history():
            current = getattr(agent, "session", {})
            current = current if isinstance(current, dict) else {}
            history = _history(
                active_prompt_history(current.get("messages", []))
            )
            session.history = history
            if hasattr(session, "default_buffer"):
                session.default_buffer.history = history

        try:
            try:
                return handle_input(
                    agent,
                    user_input,
                    confirm=confirm,
                    render_answer=renderer.answer,
                    render_error=lambda text: renderer.notice(text, error=True),
                    refresh_history=refresh_history,
                )
            except KeyboardInterrupt as exc:
                if hasattr(exc, "signal_number"):
                    raise
                renderer.notice("request interrupted")
                return None
        finally:
            refresh_history()

    try:
        while True:
            try:
                user_input = session.prompt(
                    renderer.prompt(),
                    prompt_continuation=_continuation,
                    bottom_toolbar=lambda: renderer.toolbar(agent, model=model),
                ).strip()
            except EOFError:
                return 0
            except KeyboardInterrupt:
                now = time.monotonic()
                if now - last_interrupt <= _DOUBLE_INTERRUPT_SECONDS:
                    return 130
                last_interrupt = now
                renderer.notice("press Ctrl+C again to exit")
                continue

            if user_input:
                renderer.user(user_input)
            result = process(user_input)
            if result is not None:
                return result
    finally:
        try:
            renderer.close()
        except Exception:  # noqa: BLE001 - UI cleanup cannot hide the primary result
            pass
        agent._trace_listener = previous_listener
        agent._approval_prompt = previous_approval_prompt
