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
from pony.tui.render import FULL_TUI_MINIMUM_COLUMNS, TuiRenderer


_WINDOWS = os.name == "nt"
_DOUBLE_INTERRUPT_SECONDS = 1.5
_MAX_EDITOR_LINES = 6
_COMPLETION_ROWS = 5


def _terminal_width_message():
    return (
        "Terminal too narrow\n"
        f"Expand to at least {FULL_TUI_MINIMUM_COLUMNS} columns to continue.\n"
    )


def _application_terminal_columns(app):
    output = getattr(app, "output", None)
    get_size = getattr(output, "get_size", None)
    if not callable(get_size):
        return None
    return get_size().columns


def _session_terminal_columns(session):
    columns = _application_terminal_columns(getattr(session, "app", None))
    if columns is not None:
        return columns
    return shutil.get_terminal_size((80, 24)).columns


def _normalize_prompt_text(value):
    text = str(value)
    if not any("\ud800" <= character <= "\udfff" for character in text):
        return text
    return text.encode("utf-16-le", "surrogatepass").decode(
        "utf-16-le",
        "replace",
    )


class _PromptTextAssembler:
    """Merge UTF-16 surrogate records that arrive in separate input batches."""

    def __init__(self):
        self._pending_high_surrogate = ""

    def feed(self, value):
        text = self._pending_high_surrogate + str(value)
        self._pending_high_surrogate = ""
        if text and "\ud800" <= text[-1] <= "\udbff":
            self._pending_high_surrogate = text[-1]
            text = text[:-1]
        return _normalize_prompt_text(text)

    def flush(self):
        if not self._pending_high_surrogate:
            return ""
        self._pending_high_surrogate = ""
        return "\ufffd"

    def reset(self):
        self._pending_high_surrogate = ""


def _install_surrogate_safe_buffer(buffer):
    if getattr(buffer, "_pony_surrogate_safe", False):
        return buffer
    assembler = _PromptTextAssembler()
    # PromptSession owns Buffer construction; wrap its two mutation boundaries
    # instead of copying prompt-toolkit's private Buffer factory.
    original_insert = buffer.insert_text
    original_reset = buffer.reset

    def insert_text(
        data,
        overwrite=False,
        move_cursor=True,
        fire_event=True,
    ):
        text = assembler.feed(data)
        if text:
            original_insert(
                text,
                overwrite=overwrite,
                move_cursor=move_cursor,
                fire_event=fire_event,
            )

    def reset(*args, **kwargs):
        assembler.reset()
        return original_reset(*args, **kwargs)

    def flush_pending_text():
        text = assembler.flush()
        if text:
            original_insert(text)

    buffer.insert_text = insert_text
    buffer.reset = reset
    buffer._pony_flush_pending_text = flush_pending_text
    buffer._pony_surrogate_safe = True
    return buffer


def _flush_pending_prompt_text(buffer):
    flush = getattr(buffer, "_pony_flush_pending_text", None)
    if flush is not None:
        flush()


def _install_startup_resize_repaint(session, startup_is_visible):
    # Windows Terminal reflows old wide rows before prompt-toolkit can redraw.
    if not _WINDOWS:
        return None
    app = getattr(session, "app", None)
    after_render = getattr(app, "after_render", None)
    if after_render is None:
        return None
    previous_columns = None

    def repaint(resized_app):
        nonlocal previous_columns
        columns = resized_app.output.get_size().columns
        changed = previous_columns is not None and columns != previous_columns
        previous_columns = columns
        if changed and startup_is_visible():
            resized_app.renderer.clear()
            resized_app.invalidate()

    after_render += repaint
    return repaint


def _install_activity_resize_repaint(session, on_resize):
    app = getattr(session, "app", None)
    after_render = getattr(app, "after_render", None)
    columns = _application_terminal_columns(app)
    if after_render is None or columns is None:
        return None

    previous_columns = columns

    def repaint(resized_app):
        nonlocal previous_columns
        columns = resized_app.output.get_size().columns
        if columns == previous_columns:
            return
        previous_columns = columns
        try:
            on_resize(resized_app, columns)
        except Exception:  # resize repaint cannot replace the active turn outcome
            pass

    after_render += repaint
    return repaint


class _PreviewCoalescer:
    """Synchronize the first preview, then keep one rate-limited latest slot."""

    def __init__(self, render_first, render_later, schedule, *, clock=time.monotonic):
        self._render_first = render_first
        self._render_later = render_later
        self._schedule = schedule
        self._clock = clock
        self._lock = threading.Lock()
        self._generation = 0
        self._active = False
        self._first_pending = True
        self._latest = None
        self._scheduled = False
        self._last_rendered_at = 0.0

    def begin(self):
        with self._lock:
            self._generation += 1
            self._active = True
            self._first_pending = True
            self._latest = None
            self._scheduled = False
            self._last_rendered_at = 0.0

    def submit(self, snapshot):
        with self._lock:
            if not self._active:
                return False
            if self._first_pending:
                self._first_pending = False
                generation = self._generation
                first = True
            else:
                self._latest = str(snapshot)
                if self._scheduled:
                    return True
                self._scheduled = True
                generation = self._generation
                delay = max(0.0, 0.1 - (self._clock() - self._last_rendered_at))
                first = False
        if first:
            try:
                displayed = self._render_first(snapshot) is True
            except Exception:
                displayed = False
            with self._lock:
                if not self._active or generation != self._generation:
                    return False
                if displayed:
                    self._last_rendered_at = self._clock()
                else:
                    self._active = False
            return displayed
        return self._schedule_flush(generation, delay)

    def finish(self):
        with self._lock:
            self._generation += 1
            self._active = False
            self._latest = None
            self._scheduled = False

    def _schedule_flush(self, generation, delay):
        try:
            scheduled = self._schedule(
                delay,
                lambda: self._flush(generation),
            ) is True
        except Exception:
            scheduled = False
        if scheduled:
            return True
        with self._lock:
            if generation == self._generation:
                self._active = False
                self._latest = None
                self._scheduled = False
        return False

    def _flush(self, generation):
        with self._lock:
            if not self._active or generation != self._generation:
                return
            snapshot = self._latest
            self._latest = None
            self._scheduled = False
        if snapshot is None:
            return
        try:
            displayed = self._render_later(snapshot) is True
        except Exception:
            displayed = False
        with self._lock:
            if not self._active or generation != self._generation:
                return
            if not displayed:
                self._active = False
                self._latest = None
                return
            self._last_rendered_at = self._clock()
            if self._latest is None or self._scheduled:
                return
            self._scheduled = True
            next_generation = self._generation
        self._schedule_flush(next_generation, 0.1)


class _CompactPromptSession(PromptSession):
    """Keep the multiline editor inline with native terminal scrollback."""

    def _create_default_buffer(self):
        return _install_surrogate_safe_buffer(super()._create_default_buffer())

    def _get_default_buffer_control_height(self):
        if self.default_buffer.complete_state is not None:
            return Dimension(
                min=self.reserve_space_for_menu,
                max=self.reserve_space_for_menu,
            )
        lines = min(self.default_buffer.document.line_count, _MAX_EDITOR_LINES)
        return Dimension(min=lines, max=lines)


def tui_capability(*, stdin=None, stdout=None, environ=None, columns=None):
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    environ = os.environ if environ is None else environ
    if not getattr(stdin, "isatty", lambda: False)():
        return False, ""
    if not getattr(stdout, "isatty", lambda: False)():
        return False, ""
    term = environ.get("TERM", "").strip()
    if term.casefold() == "dumb" or (not term and not _WINDOWS):
        return False, "terminal cannot display the PONY CODE conversation interface"
    width = columns
    if width is None:
        width = shutil.get_terminal_size((80, 24)).columns
    if width < FULL_TUI_MINIMUM_COLUMNS:
        return False, (
            "terminal width must be at least "
            f"{FULL_TUI_MINIMUM_COLUMNS} columns for the required "
            "full-size PONY CODE logo"
        )
    return True, ""


def should_use_tui(*, stdin=None, stdout=None, environ=None, columns=None):
    enabled, _reason = tui_capability(
        stdin=stdin,
        stdout=stdout,
        environ=environ,
        columns=columns,
    )
    return enabled


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
        columns = _application_terminal_columns(getattr(event, "app", None))
        if columns is not None and columns < FULL_TUI_MINIMUM_COLUMNS:
            event.app.invalidate()
            return
        buffer = event.current_buffer
        _flush_pending_prompt_text(buffer)
        if buffer.document.text_before_cursor.endswith("\\"):
            buffer.delete_before_cursor(1)
            buffer.insert_text("\n")
            return
        buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def insert_newline(event):
        _flush_pending_prompt_text(event.current_buffer)
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
    if getattr(agent, "stream_enabled", False):
        from pony.runtime.options import require_streaming_client

        require_streaming_client(getattr(agent, "model_client", None))
    renderer = TuiRenderer(
        no_color=no_color or os.environ.get("NO_COLOR") is not None,
    )
    startup_visible = show_header or resume_projection is not None
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
    _install_startup_resize_repaint(session, lambda: startup_visible)
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

    def render_later(snapshot):
        with ui_lock:
            return renderer.stream_preview(snapshot)

    def schedule_preview(delay, callback):
        app = getattr(session, "app", None)
        loop = getattr(app, "loop", None)
        if app is None or loop is None or not getattr(app, "is_running", False):
            return False

        async def render_in_terminal():
            try:
                await run_in_terminal(callback)
            except Exception:
                pass

        def enqueue_render():
            if getattr(app, "is_running", False):
                loop.create_task(render_in_terminal())

        def schedule_on_loop():
            if getattr(app, "is_running", False):
                loop.call_later(delay, enqueue_render)

        loop.call_soon_threadsafe(schedule_on_loop)
        return True

    preview_coalescer = _PreviewCoalescer(
        lambda snapshot: call_ui(renderer.stream_preview, snapshot),
        render_later,
        schedule_preview,
    )

    def stream_committed():
        preview_coalescer.begin()
        call_ui(renderer.stream_committed)

    def stream_finished():
        preview_coalescer.finish()
        call_ui(renderer.stream_finished)

    def schedule_activity_resize(resized_app, _columns):
        async def repaint_in_terminal():
            def repaint():
                with ui_lock:
                    renderer.resize(resized_app.output.get_size().columns)

            try:
                return await run_in_terminal(repaint)
            except Exception:  # resize repaint cannot replace the active turn outcome
                return None

        loop = getattr(resized_app, "loop", None)
        if loop is not None and getattr(resized_app, "is_running", False):
            loop.create_task(repaint_in_terminal())

    _install_activity_resize_repaint(session, schedule_activity_resize)

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
        answer = _normalize_prompt_text(
            session.prompt(
                FormattedText([("class:warning", message)]),
                multiline=False,
                bottom_toolbar=None,
            )
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

    def prompt_message():
        columns = _session_terminal_columns(session)
        if startup_visible and columns < FULL_TUI_MINIMUM_COLUMNS:
            return FormattedText([("class:warning", _terminal_width_message())])
        if not startup_visible:
            return renderer.prompt(
                columns=columns,
                pending=input_queue.pending_count,
            )
        fragments = []
        if show_header:
            fragments.extend(renderer.welcome(agent, model=model, columns=columns))
        if resume_projection is not None:
            fragments.extend(renderer.resume_card(resume_projection))
        fragments.extend(renderer.prompt(columns=columns))
        return FormattedText(fragments)

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
        on_start=lambda text: call_ui(renderer.turn_started, text),
        on_wake=wake_prompt,
    )

    def approve(name, args):
        accepted = input_queue.confirm(
            "  Approve once? [y/N] ",
            on_ready=lambda: call_ui(renderer.approval, name, args),
        )
        call_ui(renderer.approval_resolved, accepted)
        return accepted

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

    def render_status(text):
        if not str(text).startswith("queued for next turn: "):
            call_ui(
                renderer.notice,
                text,
                restore_activity=input_queue.busy,
            )

    def render_user(text):
        call_ui(
            renderer.user,
            text,
            restore_activity=input_queue.busy,
        )

    def render_error(text):
        call_ui(
            renderer.notice,
            text,
            error=True,
            restore_activity=input_queue.busy,
        )

    from pony.cli.start import _raise_or_return_terminal, _route_repl_input

    previous_listener = getattr(agent, "_trace_listener", None)
    previous_approval_prompt = getattr(agent, "_approval_prompt", None)
    previous_stream_committed = getattr(agent, "_stream_committed_callback", None)
    previous_stream_preview = getattr(agent, "_stream_preview_callback", None)
    previous_stream_finished = getattr(agent, "_stream_finished_callback", None)
    agent._trace_listener = lambda envelope: call_ui(renderer.trace, envelope)
    agent._approval_prompt = approve
    if getattr(agent, "stream_enabled", False):
        agent._stream_committed_callback = stream_committed
        agent._stream_preview_callback = preview_coalescer.submit
        agent._stream_finished_callback = stream_finished
    last_interrupt = 0.0

    try:
        while True:
            terminal_result = _raise_or_return_terminal(input_queue)
            if terminal_result is not None:
                return terminal_result
            refresh_history()
            confirmation = input_queue.confirmation()
            try:
                user_input = _normalize_prompt_text(
                    session.prompt(
                        FormattedText([("class:warning", confirmation)])
                        if confirmation is not None
                        else prompt_message,
                        prompt_continuation=_continuation,
                        bottom_toolbar=lambda: renderer.toolbar(
                            agent,
                            model=model,
                        ),
                    )
                )
                startup_visible = False
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
                        "current turn continues (request cancellation is unavailable); "
                        f"cleared {removed} queued next-turn input(s)",
                        restore_activity=True,
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
                render_user=render_user,
                render_status=render_status,
                render_error=render_error,
                confirmation_shown=confirmation is not None,
            )
            refresh_history()
            if result is not None:
                return result
    finally:
        input_queue.close()
        preview_coalescer.finish()
        try:
            call_ui(renderer.close)
        except Exception:  # noqa: BLE001 - UI cleanup cannot hide the primary result
            pass
        agent._trace_listener = previous_listener
        agent._approval_prompt = previous_approval_prompt
        agent._stream_committed_callback = previous_stream_committed
        agent._stream_preview_callback = previous_stream_preview
        agent._stream_finished_callback = previous_stream_finished
