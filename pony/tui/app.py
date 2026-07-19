"""Prompt-toolkit shell around Pony's existing REPL semantics."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension
from prompt_toolkit.shortcuts import choice, CompleteStyle

from pony.cli.help import SLASH_COMMANDS
from pony.cli.input_queue import InputQueue
from pony.runtime.resume import active_prompt_history
from pony.tools.permissions import display_permission_mode
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

    def __init__(self, agent=None):
        self.agent = agent

    def get_completions(self, document, _complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or any(character.isspace() for character in text):
            return
        commands = list(SLASH_COMMANDS)
        catalog = getattr(self.agent, "project_skills", None)
        for skill in getattr(catalog, "skills", ()):
            commands.append(
                type(SLASH_COMMANDS[0])(
                    f"/{skill.name}",
                    f"/{skill.name} [prompt]",
                    skill.description,
                )
            )
        for command in commands:
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


def _permission_picker(agent, rules, tools, *, choose=choice, style=None):
    current_rules = {key: list(rules.get(key, ())) for key in ("allow", "ask", "deny")}
    current_mode = agent.current_permission_mode()
    selections = []
    while True:
        tool_behavior = {
            name: next(
                (behavior for behavior, names in current_rules.items() if name in names),
                "default",
            )
            for name in tools
        }
        action = choose(
            "Permissions",
            options=[
                ("done", "Done"),
                ("mode", f"Mode · {display_permission_mode(current_mode)}"),
                *[
                    (f"tool:{name}", f"{name} · {tool_behavior[name]}")
                    for name in tools
                ],
            ],
            default="done",
            style=style,
            symbol=">",
        )
        if action == "done":
            return selections or None
        if action == "mode":
            modes = ["manual", "auto", "acceptEdits", "dontAsk", "plan"]
            if agent.bypass_permissions_available:
                modes.insert(3, "bypassPermissions")
            current_mode = choose(
                "Permission mode",
                options=[(mode, mode) for mode in modes],
                default=display_permission_mode(current_mode),
                style=style,
                symbol=">",
            )
            selections = [item for item in selections if item[0] != "mode"]
            selections.append(("mode", current_mode))
            continue
        name = action.removeprefix("tool:")
        behavior = choose(
            f"Rule for {name}",
            options=[
                ("allow", "Allow"),
                ("ask", "Ask"),
                ("deny", "Deny"),
                ("remove", "Use mode default"),
            ],
            default=tool_behavior[name] if tool_behavior[name] != "default" else "remove",
            style=style,
            symbol=">",
        )
        for names in current_rules.values():
            if name in names:
                names.remove(name)
        if behavior != "remove":
            current_rules[behavior].append(name)
        selections = [item for item in selections if item[0] == "mode" or item[1] != name]
        selections.append((behavior, name))


def _session_picker(command, candidates, *, choose=choice, style=None):
    selected = choose(
        f"{command[1:].capitalize()} session from",
        options=[("", "Cancel"), *candidates],
        default="",
        style=style,
        symbol=">",
    )
    return selected or None


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
        completer=SlashCommandCompleter(agent),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        erase_when_done=True,
        enable_history_search=True,
        key_bindings=_key_bindings(),
        multiline=True,
        reserve_space_for_menu=_COMPLETION_ROWS,
        style=renderer.style,
    )
    ui_thread = threading.current_thread()
    ui_lock = threading.RLock()

    def call_ui(callback, *args, **kwargs):
        def render():
            with ui_lock:
                return callback(*args, **kwargs)

        async def render_in_app():
            return await run_in_terminal(render)

        app = getattr(session, "app", None)
        if (
            threading.current_thread() is ui_thread
            or app is None
            or not app.is_running
            or app.loop is None
        ):
            return render()
        future = asyncio.run_coroutine_threadsafe(
            render_in_app(),
            app.loop,
        )
        return future.result()

    wake_result = object()

    def wake_prompt():
        app = getattr(session, "app", None)
        if app is None or not app.is_running or app.loop is None:
            return

        def wake():
            if app.is_running:
                app.exit(result=wake_result)

        app.loop.call_soon_threadsafe(wake)

    def confirm(message):
        answer = session.prompt(
            FormattedText([("class:warning", message)]),
            multiline=False,
            bottom_toolbar=None,
        )
        return answer.strip().casefold() in {"y", "yes"}

    def manage_permissions(rules, tools):
        return _permission_picker(agent, rules, tools, style=renderer.style)

    def pick_session_entry(command, candidates):
        return _session_picker(command, candidates, style=renderer.style)

    def refresh_history():
        current = getattr(agent, "session", {})
        current = current if isinstance(current, dict) else {}
        history = _history(active_prompt_history(current.get("messages", [])))
        session.history = history
        if hasattr(session, "default_buffer"):
            session.default_buffer.history = history

    input_queue = None

    def process_turn(user_input):
        try:
            return handle_input(
                agent,
                user_input,
                confirm=input_queue.confirm,
                render_answer=lambda text: call_ui(renderer.answer, text),
                render_error=lambda text: call_ui(
                    renderer.notice,
                    text,
                    error=True,
                ),
                refresh_history=lambda: None,
            )
        except KeyboardInterrupt as exc:
            if hasattr(exc, "signal_number"):
                raise
            call_ui(renderer.notice, "request interrupted")
            return None
        finally:
            try:
                call_ui(refresh_history)
            except Exception:  # noqa: BLE001 - UI refresh cannot replace turn outcome
                pass

    input_queue = InputQueue(
        process_turn,
        on_start=lambda text: call_ui(renderer.user, text),
        on_wake=wake_prompt,
    )

    def approve(name, args):
        call_ui(renderer.approval, name, args)
        return input_queue.confirm("  Approve once? [y/N] ")

    def process_local(user_input):
        return handle_input(
            agent,
            user_input,
            confirm=confirm,
            render_answer=lambda text: call_ui(renderer.answer, text),
            render_error=lambda text: call_ui(renderer.notice, text, error=True),
            refresh_history=refresh_history,
            manage_permissions=manage_permissions,
            pick_session_entry=pick_session_entry,
        )

    from pony.cli.start import _raise_or_return_terminal, _route_repl_input

    previous_listener = getattr(agent, "_trace_listener", None)
    previous_approval_prompt = getattr(agent, "_approval_prompt", None)
    agent._trace_listener = lambda envelope: call_ui(renderer.trace, envelope)
    agent._approval_prompt = approve
    last_interrupt = 0.0

    try:
        while True:
            terminal_result = _raise_or_return_terminal(input_queue)
            if terminal_result is not None:
                return terminal_result
            refresh_history()
            confirmation = input_queue.confirmation()
            try:
                user_input = session.prompt(
                    (
                        FormattedText([("class:warning", confirmation)])
                        if confirmation is not None
                        else renderer.prompt()
                    ),
                    prompt_continuation=_continuation,
                    bottom_toolbar=lambda: renderer.toolbar(agent, model=model),
                )
            except EOFError:
                input_queue.close()
                terminal_result = _raise_or_return_terminal(input_queue)
                if terminal_result is not None:
                    return terminal_result
                return 0
            except KeyboardInterrupt as exc:
                if input_queue.busy and not hasattr(exc, "signal_number"):
                    input_queue.answer_confirmation("")
                    removed = input_queue.clear()
                    call_ui(
                        renderer.notice,
                        f"current turn continues; cleared {removed} pending"
                    )
                    continue
                if hasattr(exc, "signal_number"):
                    raise
                now = time.monotonic()
                if now - last_interrupt <= _DOUBLE_INTERRUPT_SECONDS:
                    return 130
                last_interrupt = now
                call_ui(renderer.notice, "press Ctrl+C again to exit")
                continue

            if user_input is wake_result:
                continue
            user_input = user_input.strip()
            result = _route_repl_input(
                agent,
                input_queue,
                user_input,
                process_local=process_local,
                render_user=lambda text: call_ui(renderer.user, text),
                render_status=lambda text: call_ui(renderer.notice, text),
                render_error=lambda text: call_ui(
                    renderer.notice,
                    text,
                    error=True,
                ),
            )
            refresh_history()
            if result is not None:
                return result
    finally:
        input_queue.close()
        try:
            call_ui(renderer.close)
        except Exception:  # noqa: BLE001 - UI cleanup cannot hide the primary result
            pass
        agent._trace_listener = previous_listener
        agent._approval_prompt = previous_approval_prompt
