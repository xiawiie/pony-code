import io
import queue
import threading
from types import SimpleNamespace

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.utils import get_cwidth

import pony.tui.app as tui_app
from pony.agent.observability import project_trace_event
from pony.cli.start import run_repl
from pony.providers.transport import ProviderTransportError
from pony.tui.app import (
    SlashCommandCompleter,
    _CompactPromptSession,
    _key_bindings,
    _history,
    _permission_picker,
    _session_picker,
    run_tui,
    should_use_tui,
)
from pony.tui.render import _COLOR_STYLE, _PLAIN_STYLE, TuiRenderer, logo_text


class _Stream:
    def __init__(self, is_tty):
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty", "term", "columns", "expected"),
    (
        (True, True, "xterm-256color", 80, False),
        (True, True, "xterm-256color", 111, False),
        (True, True, "xterm-256color", 112, True),
        (False, True, "xterm-256color", 80, False),
        (True, False, "xterm-256color", 80, False),
        (True, True, None, 80, False),
        (True, True, " ", 80, False),
        (True, True, "dumb", 80, False),
        (True, True, "xterm-256color", 79, False),
    ),
)
def test_tui_requires_a_capable_interactive_terminal(
    monkeypatch,
    stdin_tty,
    stdout_tty,
    term,
    columns,
    expected,
):
    monkeypatch.setattr("pony.tui.app._WINDOWS", False)

    assert should_use_tui(
        stdin=_Stream(stdin_tty),
        stdout=_Stream(stdout_tty),
        environ={} if term is None else {"TERM": term},
        columns=columns,
    ) is expected


def test_windows_tui_does_not_require_term(monkeypatch):
    monkeypatch.setattr("pony.tui.app._WINDOWS", True)

    enabled, reason = tui_app.tui_capability(
        stdin=_Stream(True),
        stdout=_Stream(True),
        environ={},
        columns=80,
    )
    assert not enabled
    assert reason == (
        "terminal width must be at least 112 columns for the required "
        "full-size PONY CODE logo"
    )
    assert should_use_tui(
        stdin=_Stream(True),
        stdout=_Stream(True),
        environ={},
        columns=112,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("plain", "plain"),
        ("\ud83d\udc34", "\U0001f434"),
        ("left \ud83d\udc34 right", "left \U0001f434 right"),
        ("\ud83d middle \udc34", "\ufffd middle \ufffd"),
    ),
)
def test_prompt_text_normalizes_windows_surrogate_input(value, expected):
    assert tui_app._normalize_prompt_text(value) == expected


def test_prompt_buffer_merges_surrogates_before_rendering():
    buffer = tui_app._install_surrogate_safe_buffer(Buffer())

    buffer.insert_text("\ud83d")
    assert buffer.text == ""

    buffer.insert_text("\udc34")
    assert buffer.text == "\U0001f434"
    assert buffer.cursor_position == 1

    buffer.insert_text(" left \ud83d")
    assert buffer.text == "\U0001f434 left "
    tui_app._flush_pending_prompt_text(buffer)
    assert buffer.text == "\U0001f434 left \ufffd"

    reset_buffer = tui_app._install_surrogate_safe_buffer(Buffer())
    reset_buffer.insert_text("\ud83d")
    reset_buffer.reset()
    reset_buffer.insert_text("\udc34")
    assert reset_buffer.text == "\ufffd"


def test_tui_routes_normalized_non_bmp_text_to_the_turn(monkeypatch):
    received = []

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def prompt(self, *_args, **_kwargs):
            return "中文 \ud83d\udc34 English"

    agent = SimpleNamespace(
        _trace_listener=None,
        _approval_prompt=None,
        current_permission_mode=lambda: "auto",
        project_skill=lambda _name: None,
        model_client=SimpleNamespace(provider="openai"),
        workspace=SimpleNamespace(cwd="/repo", branch="main"),
        session={"messages": []},
    )

    def route_input(_agent, _input_queue, text, **_kwargs):
        received.append(text)
        return 0

    monkeypatch.setattr("pony.tui.app._CompactPromptSession", FakeSession)
    monkeypatch.setattr("pony.cli.start._route_repl_input", route_input)
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda *_args, **_kwargs: None,
    )

    assert run_tui(agent, model="gpt-test", no_color=True, handle_input=lambda *_a, **_k: 0) == 0
    assert received == ["中文 \U0001f434 English"]


@pytest.mark.parametrize("columns", (40, 80, 111, 112, 120))
def test_terminal_logo_only_exposes_the_full_size_asset(columns):
    rendered = logo_text(columns)

    lines = rendered.splitlines()
    assert "⣿" in rendered and "█" in rendered
    assert len(lines) == 11
    if columns >= 112:
        assert max(get_cwidth(line) for line in lines) < columns
    else:
        assert rendered == logo_text(112)


def test_terminal_logo_default_is_the_full_size_asset():
    assert logo_text() == logo_text(120)


@pytest.mark.parametrize("columns", (80, 111, 112, 140))
def test_terminal_welcome_preserves_full_brand_and_status(columns, monkeypatch):
    output = []
    renderer = TuiRenderer(no_color=True)
    agent = SimpleNamespace(
        current_permission_mode=lambda: "default",
        model_client=SimpleNamespace(
            provider_metadata={"protocol_family": "openai_chat_completions"}
        ),
        session={},
    )
    monkeypatch.setattr("pony.tui.render._product_version", lambda: "1.2.3")
    renderer._write = lambda value, **_kwargs: output.append(value)

    renderer.header(agent, model="gpt-test", columns=columns)

    rendered = "".join(fragment[1] for fragment in output[0])
    assert "⣿" in rendered and "█" in rendered
    assert "v1.2.3" in rendered
    assert "Ready" in rendered
    assert "openai/chat/gpt-test" in rendered
    assert "Local coding agent for repository-grounded work" in rendered
    assert "permission manual" in rendered
    if columns >= 112:
        assert all(get_cwidth(line) < columns for line in rendered.splitlines())


def test_tui_chrome_is_monochrome_but_status_colors_keep_their_meaning():
    rules = dict(_COLOR_STYLE.style_rules)
    all_rules = " ".join(rules.values())

    for name in ("logo", "editor.prompt", "key"):
        assert "#" not in rules[name]
    assert rules["editor.border"] == "#777777"
    assert "#002fa7" not in all_rules
    assert "#d71920" not in all_rules
    assert "#d75f5f" not in all_rules
    assert rules["error"] == "bold #ff4d4f"
    assert rules["warning"] == "bold #d29922"
    plain_rules = dict(_PLAIN_STYLE.style_rules)
    assert plain_rules["user"] == ""
    assert plain_rules["user.rail"] == ""
    assert plain_rules["assistant.label"] == ""
    assert not any(
        any(token.startswith("bg:") or token == "reverse" for token in rule.split())
        for rule in plain_rules.values()
    )


def test_slash_completion_is_generated_from_documented_commands():
    completions = list(
        SlashCommandCompleter().get_completions(Document("/memory-r"), None)
    )

    assert [item.text for item in completions] == ["/memory-review"]
    assert completions[0].display_text == "/memory-review"
    assert "/save" not in {
        item.text
        for item in SlashCommandCompleter().get_completions(Document("/"), None)
    }
    assert "/todo" not in {
        item.text
        for item in SlashCommandCompleter().get_completions(Document("/"), None)
    }
    assert {"/permissions", "/allowed-tools", "/model", "/plan", "/queue"} <= {
        item.text
        for item in SlashCommandCompleter().get_completions(Document("/"), None)
    }
    assert "/mode" not in {
        item.text
        for item in SlashCommandCompleter().get_completions(Document("/"), None)
    }


def test_tui_history_uses_only_supplied_canonical_prompts():
    assert _history(["first", "active branch"]).get_strings() == [
        "first",
        "active branch",
    ]


def test_tui_permission_picker_navigates_tools_rules_and_modes():
    answers = iter(
        ["tool:write_file", "deny", "mode", "manual", "done"]
    )
    prompts = []

    def choose(message, *, options, **_kwargs):
        prompts.append((message, dict(options)))
        return next(answers)

    agent = SimpleNamespace(
        current_permission_mode=lambda: "auto",
        bypass_permissions_available=False,
    )

    selections = _permission_picker(
        agent,
        {"allow": [], "ask": [], "deny": []},
        ["read_file", "write_file"],
        choose=choose,
    )

    assert selections == [("deny", "write_file"), ("mode", "manual")]
    assert "write_file · default" in prompts[0][1].values()
    assert "write_file · deny" in prompts[2][1].values()
    assert "bypassPermissions" not in prompts[3][1]


def test_tui_session_picker_offers_cancel_and_selected_entry():
    prompts = []

    def choose(message, *, options, **_kwargs):
        prompts.append((message, options))
        return "entry-1"

    selected = _session_picker(
        "/rewind",
        [("entry-1", "entry-1 | user: investigate | active")],
        choose=choose,
    )

    assert selected == "entry-1"
    assert prompts == [
        (
            "Rewind session from",
            [("", "Cancel"), ("entry-1", "entry-1 | user: investigate | active")],
        )
    ]


def test_tui_resume_card_labels_fact_sources(monkeypatch):
    output = []
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )
    projection = {
        "permission_mode": "plan",
        "goal": {"text": "Ship", "source": "checkpoint"},
        "checkpoint": {"status": "ready", "blocker": "", "next_steps": []},
        "resume": {"status": "ready"},
        "model": {
            "protocol_family": "anthropic_messages",
            "model": "claude-test",
        },
    }

    TuiRenderer(no_color=True).resume(projection)

    rendered = "".join(fragment[1] for fragment in output[0])
    assert "permission [session]: plan" in rendered
    assert "goal [checkpoint]: Ship" in rendered
    assert "checkpoint [checkpoint]: status=ready" in rendered
    assert "model [provider_binding]: anthropic_messages/claude-test" in rendered


def test_tui_plan_approval_renders_the_complete_artifact(monkeypatch):
    output = []
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )
    plan = "# Plan\n\n" + ("inspect and verify\n" * 80) + "FINAL APPROVED STEP"

    TuiRenderer(no_color=True).approval(
        "exit_plan_mode",
        {"plan": plan, "revision": 7},
    )

    rendered = "".join(fragment[1] for value in output for fragment in value)
    assert "PLAN APPROVAL REQUIRED" in rendered
    assert "revision 7" in rendered
    assert "FINAL APPROVED STEP" in rendered


@pytest.mark.parametrize(
    ("text", "expected"),
    (("submit", [("submit", None)]), ("continue\\", [("delete", 1), ("insert", "\n")])),
)
def test_enter_binding_submits_or_inserts_an_explicit_newline(text, expected):
    calls = []
    buffer = SimpleNamespace(
        document=SimpleNamespace(text_before_cursor=text),
        delete_before_cursor=lambda count: calls.append(("delete", count)),
        insert_text=lambda value: calls.append(("insert", value)),
        validate_and_handle=lambda: calls.append(("submit", None)),
    )
    enter = next(
        binding
        for binding in _key_bindings().bindings
        if binding.handler.__name__ == "submit"
    )

    enter.handler(SimpleNamespace(current_buffer=buffer))

    assert calls == expected


def test_enter_binding_preserves_input_while_terminal_is_too_narrow():
    calls = []
    buffer = SimpleNamespace(
        document=SimpleNamespace(text_before_cursor="keep this input"),
        validate_and_handle=lambda: calls.append("submit"),
    )
    app = SimpleNamespace(
        output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=111)),
        invalidate=lambda: calls.append("invalidate"),
    )
    enter = next(
        binding
        for binding in _key_bindings().bindings
        if binding.handler.__name__ == "submit"
    )

    enter.handler(SimpleNamespace(current_buffer=buffer, app=app))

    assert calls == ["invalidate"]
    assert buffer.document.text_before_cursor == "keep this input"


def test_slash_key_opens_command_menu_at_start_of_input():
    calls = []
    document = SimpleNamespace(text_before_cursor="")

    def insert_text(value):
        document.text_before_cursor += value
        calls.append(("insert", value))

    buffer = SimpleNamespace(
        document=document,
        insert_text=insert_text,
        start_completion=lambda **kwargs: calls.append(("complete", kwargs)),
    )
    slash = next(
        binding for binding in _key_bindings().bindings if binding.keys == ("/",)
    )

    slash.handler(SimpleNamespace(current_buffer=buffer))

    assert calls == [("insert", "/"), ("complete", {"select_first": False})]


def test_tui_editor_grows_without_filling_the_terminal():
    session = SimpleNamespace(
        default_buffer=SimpleNamespace(
            complete_state=None,
            document=SimpleNamespace(line_count=1),
        ),
        reserve_space_for_menu=5,
    )

    assert _CompactPromptSession._get_default_buffer_control_height(session).max == 1
    session.default_buffer.document.line_count = 9
    assert _CompactPromptSession._get_default_buffer_control_height(session).max == 6


def test_tui_startup_reflows_welcome_with_current_terminal_width(monkeypatch):
    current_columns = {"value": 120}
    samples = []
    written = []

    class FakeSession:
        def __init__(self, **_kwargs):
            self.app = SimpleNamespace(
                output=SimpleNamespace(
                    get_size=lambda: SimpleNamespace(columns=current_columns["value"])
                )
            )

        def prompt(self, message, *_args, **_kwargs):
            assert callable(message)
            for columns in (140, 80, 111, 112):
                current_columns["value"] = columns
                samples.append(
                    (
                        columns,
                        "".join(fragment[1] for fragment in message()),
                    )
                )
            return "/exit"

    agent = SimpleNamespace(
        _trace_listener=None,
        _approval_prompt=None,
        current_permission_mode=lambda: "auto",
        project_skill=lambda _name: None,
        model_client=SimpleNamespace(provider="openai"),
        workspace=SimpleNamespace(cwd="/repo", branch="main"),
        session={"messages": []},
    )
    monkeypatch.setattr("pony.tui.app._CompactPromptSession", FakeSession)
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: written.append(value),
    )

    assert run_tui(agent, model="gpt-test", no_color=True, handle_input=lambda *_a, **_k: 0) == 0
    assert [columns for columns, _text in samples] == [140, 80, 111, 112]
    for columns, rendered in samples:
        if columns >= 112:
            assert "⣿" in rendered and "█" in rendered
            assert all(get_cwidth(line) < columns for line in rendered.splitlines())
            assert "Ready" in rendered
            assert rendered.endswith("\n› ")
        else:
            assert "⣿" not in rendered and "█" not in rendered
            assert "Expand to at least 112 columns to continue." in rendered
            assert "› " not in rendered
    assert not any("⣿" in "".join(fragment[1] for fragment in value) for value in written)


def test_startup_resize_repaint_clears_only_live_welcome(monkeypatch):
    handlers = []
    columns = {"value": 120}
    startup = {"visible": True}
    clears = []
    invalidations = []

    class Event:
        def __iadd__(self, handler):
            handlers.append(handler)
            return self

    app = SimpleNamespace(
        after_render=Event(),
        output=SimpleNamespace(
            get_size=lambda: SimpleNamespace(columns=columns["value"])
        ),
        renderer=SimpleNamespace(clear=lambda: clears.append(columns["value"])),
        invalidate=lambda: invalidations.append(columns["value"]),
    )
    session = SimpleNamespace(app=app)
    monkeypatch.setattr(tui_app, "_WINDOWS", True)

    tui_app._install_startup_resize_repaint(
        session,
        lambda: startup["visible"],
    )

    handlers[0](app)
    columns["value"] = 40
    handlers[0](app)
    handlers[0](app)
    startup["visible"] = False
    columns["value"] = 80
    handlers[0](app)

    assert clears == [40]
    assert invalidations == [40]


def test_startup_resize_repaint_does_not_clear_other_platforms(monkeypatch):
    handlers = []

    class Event:
        def __iadd__(self, handler):
            handlers.append(handler)
            return self

    session = SimpleNamespace(app=SimpleNamespace(after_render=Event()))
    monkeypatch.setattr(tui_app, "_WINDOWS", False)

    assert tui_app._install_startup_resize_repaint(session, lambda: True) is None
    assert handlers == []


def test_runtime_resize_reprojects_the_full_activity_source(monkeypatch):
    handlers = []
    output = []
    terminal = io.StringIO()
    columns = {"value": 120}

    class Event:
        def __iadd__(self, handler):
            handlers.append(handler)
            return self

    app = SimpleNamespace(
        after_render=Event(),
        output=SimpleNamespace(
            get_size=lambda: SimpleNamespace(columns=columns["value"])
        ),
    )
    session = SimpleNamespace(app=app)
    monkeypatch.setattr("pony.tui.render.sys.stdout", terminal)
    monkeypatch.setattr(
        "pony.tui.render._terminal_columns",
        lambda: columns["value"],
    )
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(
            "".join(fragment[1] for fragment in value)
        ),
    )
    renderer = TuiRenderer(no_color=True)
    long_path = "deep/" + "directory/" * 9 + "file.py"
    renderer.trace(
        {"event": "tool_started", "name": "read_file", "args": {"path": long_path}}
    )
    full_projection = output[-1]

    tui_app._install_activity_resize_repaint(
        session,
        lambda _app, width: renderer.resize(width),
    )
    columns["value"] = 80
    handlers[0](app)
    narrow_projection = output[-1]
    columns["value"] = 120
    handlers[0](app)

    assert narrow_projection.endswith("...")
    assert get_cwidth(narrow_projection) < 80
    assert output[-1] == full_projection
    assert renderer._activity_source == f"Reading {long_path}..."


def test_stream_preview_reuses_activity_and_reprojects_full_snapshot(monkeypatch):
    output = []
    terminal = io.StringIO()
    monkeypatch.setattr("pony.tui.render.sys.stdout", terminal)
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append("".join(part[1] for part in value)),
    )
    renderer = TuiRenderer(no_color=True)

    renderer.stream_committed()
    assert output[-1] == "Receiving..."
    assert renderer.stream_preview("first\nsecond", columns=30) is True
    assert output[-1].startswith("Pony - first second")
    renderer.notice("queue: 1 pending; turn active", restore_activity=True)
    assert renderer._activity_source == "Pony - first second"
    renderer.resize(120)

    assert renderer._activity_source == "Pony - first second"
    renderer.answer("Authoritative final", columns=120)
    assert renderer._activity_visible is False
    rendered = "".join(output)
    assert rendered.count("Pony\n") == 1
    assert rendered.count("Authoritative final") == 1


def test_stream_preview_does_not_acknowledge_only_a_truncation_ellipsis(monkeypatch):
    output = []
    monkeypatch.setattr("pony.tui.render.sys.stdout", io.StringIO())
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append("".join(part[1] for part in value)),
    )
    renderer = TuiRenderer(no_color=True)

    renderer.stream_committed()

    assert renderer.stream_preview("界界界", columns=12) is False
    assert output == ["Receiving..."]


def test_preview_coalescer_synchronizes_first_then_keeps_latest_slot():
    now = {"value": 10.0}
    first = []
    later = []
    scheduled = []
    coalescer = tui_app._PreviewCoalescer(
        lambda value: first.append(value) or True,
        lambda value: later.append(value) or True,
        lambda delay, callback: scheduled.append((delay, callback)) or True,
        clock=lambda: now["value"],
    )

    coalescer.begin()
    assert coalescer.submit("first") is True
    now["value"] = 10.02
    assert coalescer.submit("second") is True
    assert coalescer.submit("latest") is True

    assert first == ["first"]
    assert len(scheduled) == 1
    assert scheduled[0][0] == pytest.approx(0.08)
    now["value"] = 10.1
    scheduled.pop()[1]()
    assert later == ["latest"]

    coalescer.finish()
    assert coalescer.submit("too late") is False


def test_preview_coalescer_discards_a_delayed_preview_after_finish():
    later = []
    scheduled = []
    coalescer = tui_app._PreviewCoalescer(
        lambda _value: True,
        lambda value: later.append(value) or True,
        lambda _delay, callback: scheduled.append(callback) or True,
        clock=lambda: 10.0,
    )

    coalescer.begin()
    assert coalescer.submit("first") is True
    assert coalescer.submit("late") is True
    coalescer.finish()
    scheduled[0]()

    assert later == []


def test_repl_routes_a_capable_tty_to_tui(monkeypatch):
    calls = []
    agent = SimpleNamespace()
    monkeypatch.setattr("pony.tui.app.tui_capability", lambda: (True, ""))

    def fake_run_tui(received, **options):
        assert received is agent
        assert callable(options["handle_input"])
        calls.append(
            (options["model"], options["no_color"], options["show_header"])
        )
        return 0

    monkeypatch.setattr("pony.tui.app.run_tui", fake_run_tui)

    assert run_repl(agent, model="model", no_color=True) == 0
    assert calls == [("model", True, True)]


def test_repl_refuses_a_narrow_tty_instead_of_opening_plain_repl(
    monkeypatch,
    capsys,
):
    agent = SimpleNamespace()
    monkeypatch.setattr(
        "pony.tui.app.tui_capability",
        lambda: (
            False,
            "terminal width must be at least 112 columns for the required "
            "full-size PONY CODE logo",
        ),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt="": pytest.fail("plain REPL started"),
    )

    assert run_repl(agent) == 2
    assert "full-size PONY CODE logo" in capsys.readouterr().err


def test_streaming_repl_race_returns_stable_unavailable_error(monkeypatch, capsys):
    agent = SimpleNamespace(
        stream_enabled=True,
        model_client=SimpleNamespace(complete_stream=lambda: None),
        session={"messages": []},
        redact_artifact=lambda value: value,
    )
    monkeypatch.setattr(
        "pony.tui.app.tui_capability",
        lambda: (False, "terminal changed"),
    )

    assert run_repl(agent, stream=True) == 2
    assert capsys.readouterr().err == "error: streaming_unavailable\n"


def test_plain_repl_never_starts_tui(monkeypatch):
    agent = SimpleNamespace()
    monkeypatch.setattr(
        "pony.tui.app.run_tui",
        lambda *_args, **_kwargs: pytest.fail("TUI started"),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt="": (_ for _ in ()).throw(EOFError()),
    )

    assert run_repl(agent, plain=True) == 0


def test_tui_restores_runtime_hooks(monkeypatch):
    output = []
    previous_listener = object()
    previous_prompt = object()
    previous_committed = object()
    previous_preview = object()
    previous_finished = object()
    agent = SimpleNamespace(
        _trace_listener=previous_listener,
        _approval_prompt=previous_prompt,
        _stream_committed_callback=previous_committed,
        _stream_preview_callback=previous_preview,
        _stream_finished_callback=previous_finished,
        stream_enabled=True,
        current_permission_mode=lambda: "default",
        docker_sandbox=False,
        model_client=SimpleNamespace(provider="openai", complete_stream=lambda: None),
        workspace=SimpleNamespace(cwd="/repo", branch="main"),
        session={"id": "session-id"},
    )

    class FakeSession:
        def __init__(self, **_kwargs):
            self.app = SimpleNamespace(
                output=SimpleNamespace(
                    get_size=lambda: SimpleNamespace(columns=120),
                ),
            )

        def prompt(self, message, *_args, **_kwargs):
            if callable(message):
                output.append(message())
            return "/exit"

    monkeypatch.setattr("pony.tui.app._CompactPromptSession", FakeSession)
    monkeypatch.setattr(
        "pony.tui.render._terminal_columns",
        lambda: 120,
    )
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )

    def handle_input(received, text, **_kwargs):
        assert received._trace_listener is not previous_listener
        assert received._approval_prompt is not previous_prompt
        assert received._stream_committed_callback is not previous_committed
        assert received._stream_preview_callback is not previous_preview
        assert received._stream_finished_callback is not previous_finished
        assert text == "/exit"
        return 0

    assert run_tui(
        agent,
        model="gpt-test",
        no_color=True,
        handle_input=handle_input,
    ) == 0
    assert agent._trace_listener is previous_listener
    assert agent._approval_prompt is previous_prompt
    assert agent._stream_committed_callback is previous_committed
    assert agent._stream_preview_callback is previous_preview
    assert agent._stream_finished_callback is previous_finished
    header = "".join(fragment[1] for fragment in output[0])
    assert "⣿" in header
    assert "█" in header
    assert "v1.0.0" in header
    assert "Local coding agent for repository-grounded work" in header
    assert "Ready · gpt-test · permission manual" in header


def test_tui_restores_runtime_hooks_when_provider_fails(monkeypatch):
    previous_listener = object()
    previous_prompt = object()
    previous_committed = object()
    previous_preview = object()
    previous_finished = object()
    agent = SimpleNamespace(
        _trace_listener=previous_listener,
        _approval_prompt=previous_prompt,
        _stream_committed_callback=previous_committed,
        _stream_preview_callback=previous_preview,
        _stream_finished_callback=previous_finished,
        stream_enabled=True,
        current_permission_mode=lambda: "default",
        docker_sandbox=False,
        model_client=SimpleNamespace(provider="openai", complete_stream=lambda: None),
        workspace=SimpleNamespace(cwd="/repo", branch="main"),
        session={"id": "session-id"},
    )

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def prompt(self, *_args, **_kwargs):
            return "run"

    monkeypatch.setattr("pony.tui.app._CompactPromptSession", FakeSession)
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda *_args, **_kwargs: None,
    )

    def fail(*_args, **_kwargs):
        raise ProviderTransportError(
            "unsafe response",
            code="provider_protocol_mismatch",
            stage="tool_call",
            protocol_reason="tool_call_shape_invalid",
        )

    with pytest.raises(ProviderTransportError):
        run_tui(agent, model="gpt-test", no_color=True, handle_input=fail)

    assert agent._trace_listener is previous_listener
    assert agent._approval_prompt is previous_prompt
    assert agent._stream_committed_callback is previous_committed
    assert agent._stream_preview_callback is previous_preview
    assert agent._stream_finished_callback is previous_finished


def test_streaming_tui_restores_hooks_after_keyboard_interrupt(monkeypatch):
    previous = (object(), object(), object())
    agent = SimpleNamespace(
        _trace_listener=None,
        _approval_prompt=None,
        _stream_committed_callback=previous[0],
        _stream_preview_callback=previous[1],
        _stream_finished_callback=previous[2],
        stream_enabled=True,
        current_permission_mode=lambda: "default",
        model_client=SimpleNamespace(complete_stream=lambda: None),
        workspace=SimpleNamespace(cwd="/repo", branch="main"),
        session={"id": "session-id", "messages": []},
    )

    class FakeSession:
        def __init__(self, **_kwargs):
            self.app = SimpleNamespace(
                output=SimpleNamespace(
                    get_size=lambda: SimpleNamespace(columns=120),
                )
            )

        def prompt(self, *_args, **_kwargs):
            raise KeyboardInterrupt()

    monkeypatch.setattr("pony.tui.app._CompactPromptSession", FakeSession)
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda *_args, **_kwargs: None,
    )

    assert run_tui(
        agent,
        model="gpt-test",
        no_color=True,
        handle_input=lambda *_args, **_kwargs: None,
    ) == 130
    assert agent._stream_committed_callback is previous[0]
    assert agent._stream_preview_callback is previous[1]
    assert agent._stream_finished_callback is previous[2]


def test_tui_accepts_a_queued_turn_while_the_worker_is_busy(monkeypatch):
    inputs = queue.Queue()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    queued = threading.Event()
    calls = []

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def prompt(self, message, *_args, **_kwargs):
            if callable(message):
                rendered = "".join(fragment[1] for fragment in message())
                if "Widen terminal · Queued 1/5" in rendered:
                    queued.set()
            return inputs.get(timeout=3)

    agent = SimpleNamespace(
        _trace_listener=None,
        _approval_prompt=None,
        current_permission_mode=lambda: "auto",
        project_skill=lambda _name: None,
        model_client=SimpleNamespace(provider="openai"),
        workspace=SimpleNamespace(cwd="/repo", branch="main"),
        session={"messages": []},
    )

    written = []

    def write(value, **_kwargs):
        written.append("".join(fragment[1] for fragment in value))

    def handle_input(_agent, text, **_kwargs):
        if text == "/exit":
            return 0
        calls.append(text)
        if text == "first":
            first_entered.set()
            assert release_first.wait(timeout=3)
        if text == "second":
            second_finished.set()

    monkeypatch.setattr("pony.tui.app._CompactPromptSession", FakeSession)
    monkeypatch.setattr("pony.tui.render.print_formatted_text", write)
    outcome = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            run_tui(agent, model="gpt-test", no_color=True, handle_input=handle_input)
        )
    )
    thread.start()

    inputs.put("first")
    assert first_entered.wait(timeout=3)
    inputs.put("second")
    assert queued.wait(timeout=3)
    release_first.set()
    assert second_finished.wait(timeout=3)
    inputs.put("/exit")
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert outcome == [0]
    assert calls == ["first", "second"]
    assert not any("queued for next turn" in text for text in written)


def test_busy_ctrl_c_clears_queue_but_current_turn_continues(monkeypatch):
    release = threading.Event()
    current_finished = threading.Event()
    notice_visible = threading.Event()
    queue_visible = threading.Event()
    queue_activity_restored = threading.Event()
    notice_activity_restored = threading.Event()
    prompts = queue.Queue()
    calls = []

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def prompt(self, *_args, **_kwargs):
            value = prompts.get(timeout=3)
            if isinstance(value, BaseException):
                raise value
            return value

    agent = SimpleNamespace(
        _trace_listener=None,
        _approval_prompt=None,
        current_permission_mode=lambda: "auto",
        project_skill=lambda _name: None,
        model_client=SimpleNamespace(provider="openai"),
        workspace=SimpleNamespace(cwd="/repo", branch="main"),
        session={"messages": []},
    )

    def write(value, **_kwargs):
        text = "".join(fragment[1] for fragment in value)
        if "queue: 1 pending; turn active" in text:
            queue_visible.set()
        if "request cancellation is unavailable" in text:
            notice_visible.set()
        if text == "Working...":
            if notice_visible.is_set():
                notice_activity_restored.set()
            elif queue_visible.is_set():
                queue_activity_restored.set()

    def handle_input(received, text, **_kwargs):
        if text == "/exit":
            return 0
        calls.append(text)
        if text == "active":
            received._trace_listener({"event": "model_requested"})
            assert release.wait(timeout=3)
            current_finished.set()

    monkeypatch.setattr("pony.tui.app._CompactPromptSession", FakeSession)
    monkeypatch.setattr("pony.tui.render.print_formatted_text", write)
    outcome = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            run_tui(agent, model="gpt-test", no_color=True, handle_input=handle_input)
        )
    )
    thread.start()

    prompts.put("active")
    while calls != ["active"]:
        threading.Event().wait(0.01)
    prompts.put("queued")
    prompts.put("/queue")
    assert queue_activity_restored.wait(timeout=3)
    prompts.put(KeyboardInterrupt())
    assert notice_visible.wait(timeout=3)
    assert notice_activity_restored.wait(timeout=3)
    release.set()
    assert current_finished.wait(timeout=3)
    prompts.put("/exit")
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert outcome == [0]
    assert calls == ["active"]


def test_tui_routes_approval_answer_through_the_ui_prompt(monkeypatch):
    inputs = queue.Queue()
    approval_visible = threading.Event()
    approval_prompt_ready = threading.Event()
    approval_finished = threading.Event()
    decisions = []
    prompt_threads = []
    worker_threads = []
    rendered_output = []

    class FakeLoop:
        @staticmethod
        def call_soon_threadsafe(callback):
            callback()

    class ImmediateFuture:
        def __init__(self, coroutine):
            self.coroutine = coroutine

        def result(self):
            return tui_app.asyncio.run(self.coroutine)

    class FakeApp:
        is_running = True
        loop = FakeLoop()

        @staticmethod
        def exit(*, result):
            inputs.put(result)

    class FakeSession:
        def __init__(self, **_kwargs):
            self.app = FakeApp()

        def prompt(self, *args, **_kwargs):
            prompt_threads.append(threading.current_thread())
            if args and not callable(args[0]):
                prompt_text = "".join(fragment[1] for fragment in args[0])
                if "Approve once?" in prompt_text:
                    approval_prompt_ready.set()
            return inputs.get(timeout=3)

    agent = SimpleNamespace(
        _trace_listener=None,
        _approval_prompt=None,
        current_permission_mode=lambda: "auto",
        project_skill=lambda _name: None,
        model_client=SimpleNamespace(provider="openai"),
        workspace=SimpleNamespace(cwd="/repo", branch="main"),
        session={"messages": []},
    )

    def write(value, **_kwargs):
        text = "".join(fragment[1] for fragment in value)
        rendered_output.append(text)
        if "APPROVAL REQUIRED" in text:
            approval_visible.set()

    def handle_input(received, text, **_kwargs):
        if text == "/exit":
            return 0
        worker_threads.append(threading.current_thread())
        assert text == "change file"
        decisions.append(received._approval_prompt("write_file", {"path": "a.txt"}))
        approval_finished.set()

    async def run_immediately(callback):
        return callback()

    monkeypatch.setattr("pony.tui.app._CompactPromptSession", FakeSession)
    monkeypatch.setattr("pony.tui.render.print_formatted_text", write)
    monkeypatch.setattr("pony.tui.app.run_in_terminal", run_immediately)
    monkeypatch.setattr(
        "pony.tui.app.asyncio.run_coroutine_threadsafe",
        lambda coroutine, _loop: ImmediateFuture(coroutine),
    )
    outcome = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            run_tui(agent, model="gpt-test", no_color=True, handle_input=handle_input)
        )
    )
    thread.start()

    inputs.put("change file")
    assert approval_visible.wait(timeout=3)
    assert approval_prompt_ready.wait(timeout=3)
    inputs.put("yes")
    assert approval_finished.wait(timeout=3)
    inputs.put("/exit")
    thread.join(timeout=3)

    assert outcome == [0]
    assert decisions == [True]
    assert "re-enter the response" not in "".join(rendered_output)
    assert worker_threads[0] is not prompt_threads[0]
    assert all(current is prompt_threads[0] for current in prompt_threads)


def test_toolbar_is_width_bounded_and_keeps_only_essential_status():
    agent = SimpleNamespace(
        current_permission_mode=lambda: "acceptEdits",
        docker_sandbox=False,
        workspace=SimpleNamespace(
            cwd="/very/long/workspace/path/project",
            branch="feature/very-long-branch",
        ),
        session={"id": "session-must-not-appear"},
        checkpoint={"id": "checkpoint-must-not-appear"},
        model_client=SimpleNamespace(
            provider_metadata={
                "protocol_family": "anthropic_messages",
                "api_base": "https://api-must-not-appear.example",
            }
        ),
    )

    for columns in (80, 111, 112, 140):
        rendered = "".join(
            fragment[1]
            for fragment in TuiRenderer(no_color=True).toolbar(
                agent,
                model="claude-sonnet-4-6",
                columns=columns,
            )
        )
        lines = rendered.splitlines()
        assert all(get_cwidth(line) < columns for line in lines)
        footer = lines[-1]
        assert "acceptEdits" in footer
        assert "anthropic/messages/claude-sonnet-4-6" in footer
        if columns >= 112:
            assert "project" in footer
        if columns == 140:
            assert "feature/very-long-branch" in footer
        assert "/very/long" not in rendered
        assert "session-must-not-appear" not in rendered
        assert "checkpoint-must-not-appear" not in rendered
        assert "api-must-not-appear" not in rendered


def test_toolbar_reads_the_model_from_the_current_client():
    client = SimpleNamespace(
        model="gpt-next",
        provider_metadata={"protocol_family": "openai_responses"},
    )
    agent = SimpleNamespace(
        current_permission_mode=lambda: "auto",
        workspace=SimpleNamespace(cwd="/repo", branch="main"),
        model_client=client,
    )

    rendered = "".join(
        fragment[1]
        for fragment in TuiRenderer(no_color=True).toolbar(
            agent,
            model="gpt-old",
            columns=80,
        )
    )

    assert "openai/responses/gpt-next" in rendered
    assert "gpt-old" not in rendered


def test_toolbar_only_shows_repository_permission_and_model(monkeypatch):
    monkeypatch.setattr("pony.tui.render.sys.stdout", io.StringIO())
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda *_args, **_kwargs: None,
    )
    agent = SimpleNamespace(
        current_permission_mode=lambda: "auto",
        workspace=SimpleNamespace(cwd="/repo", branch="main"),
        model_client=SimpleNamespace(
            model="gpt-test",
            provider_metadata={"protocol_family": "openai_responses"},
        ),
        last_request_metadata={},
    )
    renderer = TuiRenderer(no_color=True)

    def footer():
        rendered = renderer.toolbar(
            agent,
            model="gpt-test",
            columns=140,
        )
        return "".join(fragment[1] for fragment in rendered).splitlines()[-1]

    metadata = {
        "context_breakdown": {
            "budget": {"used": 32_000, "input_limit": 128_000}
        }
    }
    task_state = SimpleNamespace(
        run_id="run_tui",
        task_id="task_tui",
        attempts=1,
    )
    prompt_built = project_trace_event(
        task_state,
        "prompt_built",
        {"request_metadata": metadata},
        created_at="now",
    )
    renderer.trace(prompt_built)
    rendered = footer()
    assert "repo (main)" in rendered
    assert "auto · openai/responses/gpt-test" in rendered
    for hidden in ("Preparing", "Waiting", "queue", "ctx", "Working"):
        assert hidden not in rendered


@pytest.mark.parametrize(
    ("terminal_event", "_expected"),
    (
        (
            {
                "event": "run_finished",
                "status": "failed",
                "stop_reason": "model_error",
            },
            "Failed",
        ),
        (
            {
                "event": "run_finished",
                "status": "stopped",
                "stop_reason": "interrupted",
            },
            "Interrupted",
        ),
    ),
)
def test_terminal_events_clear_transient_activity(
    monkeypatch,
    terminal_event,
    _expected,
):
    monkeypatch.setattr("pony.tui.render.sys.stdout", io.StringIO())
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda *_args, **_kwargs: None,
    )
    renderer = TuiRenderer(no_color=True)

    renderer.trace({"event": "model_requested"})
    renderer.trace(terminal_event)
    renderer.answer("Runtime terminal message", columns=80)

    assert not renderer._activity_visible


@pytest.mark.parametrize("columns", (80, 111, 112))
@pytest.mark.parametrize(
    ("name", "args"),
    (
        ("read_file", {"path": "deep/" + "nested-directory/" * 20 + "file.py"}),
        ("run_shell", {"command": "python -m pytest " + "very-long-test-name " * 20}),
    ),
)
def test_tool_activity_is_bounded_by_terminal_width(
    monkeypatch,
    columns,
    name,
    args,
):
    output = []
    terminal = io.StringIO()
    monkeypatch.setattr("pony.tui.render.sys.stdout", terminal)
    monkeypatch.setattr(
        "pony.tui.render._terminal_columns",
        lambda: columns,
    )
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **kwargs: output.append((value, kwargs)),
    )
    renderer = TuiRenderer(no_color=True)

    renderer.trace({"event": "tool_started", "name": name, "args": args})

    activity = "".join(fragment[1] for fragment in output[0][0])
    expected = "Reading " if name == "read_file" else "Running shell command"
    assert activity.startswith(expected)
    assert activity.endswith("...")
    assert get_cwidth(activity) < columns
    assert output[0][1]["end"] == ""

    renderer.trace({"event": "tool_executed", "tool_status": "ok"})
    cleared = terminal.getvalue().split("\r")[1]
    assert get_cwidth(cleared) == get_cwidth(activity)


def test_tool_activity_clear_uses_the_current_terminal_width(monkeypatch):
    terminal = io.StringIO()
    current = SimpleNamespace(columns=140)
    monkeypatch.setattr("pony.tui.render.sys.stdout", terminal)
    monkeypatch.setattr(
        "pony.tui.render._terminal_columns",
        lambda: current.columns,
    )
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda *_args, **_kwargs: None,
    )
    renderer = TuiRenderer(no_color=True)
    renderer.trace(
        {
            "event": "tool_started",
            "name": "read_file",
            "args": {"path": "deep/" + "long-directory/" * 40 + "a.py"},
        }
    )

    current.columns = 80
    renderer.trace({"event": "tool_executed", "tool_status": "ok"})

    cleared = terminal.getvalue().split("\r")[1]
    assert get_cwidth(cleared) == 79


@pytest.mark.parametrize("columns", (10, 80, 112))
def test_approval_details_are_bounded_by_terminal_width(monkeypatch, columns):
    output = []
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )

    TuiRenderer(no_color=True).approval(
        "run_shell",
        {"command": "测试路径/" * 200},
        columns=columns,
    )

    rendered = "".join(fragment[1] for value in output for fragment in value)
    assert all(get_cwidth(line) < columns for line in rendered.splitlines())


def test_plan_approval_is_bounded_after_an_extreme_runtime_resize(monkeypatch):
    output = []
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )

    TuiRenderer(no_color=True).approval(
        "exit_plan_mode",
        {"plan": "# Plan\nverify", "revision": 7},
        columns=10,
    )

    rendered = "".join(fragment[1] for value in output for fragment in value)
    assert all(get_cwidth(line) < 10 for line in rendered.splitlines())


def test_approval_resolution_replaces_waiting_state(monkeypatch):
    output = []
    monkeypatch.setattr("pony.tui.render.sys.stdout", io.StringIO())
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **kwargs: output.append((value, kwargs)),
    )
    renderer = TuiRenderer(no_color=True)
    renderer.trace(
        {"event": "tool_started", "name": "write_file", "args": {"path": "a.py"}}
    )
    renderer.approval("write_file", {"path": "a.py"}, columns=80)

    renderer.approval_resolved(True)

    activity = "".join(fragment[1] for fragment in output[-1][0])
    assert activity == "Writing a.py..."


def test_prompt_and_assistant_have_identity_anchors(monkeypatch):
    output = []
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )
    renderer = TuiRenderer(no_color=True)

    renderer.user("Inspect the failure", columns=80)
    renderer.answer("The failure is isolated.", columns=80)

    rendered = "".join(
        fragment[1] for value in output for fragment in value
    )
    prompt = "".join(fragment[1] for fragment in renderer.prompt(columns=80))
    assert "YOU" not in rendered
    assert "Inspect the failure" in rendered
    assert rendered.count("Pony\n") == 1
    assert "Pony\nThe failure is isolated." in rendered
    assert "Message Pony" not in prompt
    assert prompt.endswith("\n› ")


def test_editor_header_combines_runtime_width_and_queue_state():
    renderer = TuiRenderer(no_color=True)

    narrow = "".join(
        fragment[1] for fragment in renderer.prompt(columns=80, pending=2)
    )
    restored = "".join(
        fragment[1] for fragment in renderer.prompt(columns=112, pending=2)
    )
    idle = "".join(fragment[1] for fragment in renderer.prompt(columns=112))

    assert "Widen terminal · Queued 2/5\n› " in narrow
    assert "Queued 2/5\n› " in restored
    assert "Widen terminal" not in restored
    assert "Widen terminal" not in idle and "Queued" not in idle
    assert all(get_cwidth(line) < 80 for line in narrow.splitlines())


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    (
        ("read_file", {"path": "src/app.py"}, "Reading src/app.py..."),
        ("read_file", {"path": "C:\\private\\secret.py"}, "Reading secret.py..."),
        ("read_file", {"path": "C:\\"}, "Reading [path]..."),
        ("read_file", {"path": "\\\\server\\share\\"}, "Reading [path]..."),
        ("read_file", {"path": "/"}, "Reading [path]..."),
        ("memory_read", {"path": "notes.md"}, "Reading notes.md..."),
        ("read_tool_result", {"raw_result_id": "hidden"}, "Reading tool result..."),
        ("list_files", {"path": "src"}, "Listing src..."),
        ("memory_list", {}, "Listing memory..."),
        ("search", {"pattern": "needle", "path": "."}, 'Searching "needle"...'),
        ("run_shell", {"command": "secret command"}, "Running shell command..."),
        ("write_file", {"path": "src/app.py"}, "Writing src/app.py..."),
        ("patch_file", {"path": "src/app.py"}, "Patching src/app.py..."),
        ("memory_save", {"scope": "workspace"}, "Writing memory..."),
        ("delegate_worktrees", {}, "Delegating..."),
        ("custom_tool", {}, "Running custom_tool..."),
    ),
)
def test_tool_activity_uses_stable_semantic_actions(monkeypatch, name, args, expected):
    output = []
    monkeypatch.setattr("pony.tui.render.sys.stdout", io.StringIO())
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )

    TuiRenderer(no_color=True).trace(
        {"event": "tool_started", "name": name, "args": args}
    )

    assert "".join(fragment[1] for fragment in output[-1]) == expected


def test_tool_result_waits_for_the_next_model_request_before_working(monkeypatch):
    output = []
    monkeypatch.setattr("pony.tui.render.sys.stdout", io.StringIO())
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )
    renderer = TuiRenderer(no_color=True)

    renderer.trace({"event": "model_requested"})
    renderer.trace(
        {"event": "tool_started", "name": "read_file", "args": {"path": "a.py"}}
    )
    renderer.trace({"event": "tool_executed", "tool_status": "ok"})
    after_receipt = len(output)
    renderer.trace({"event": "prompt_built"})

    assert len(output) == after_receipt
    assert not renderer._activity_visible
    assert "".join(fragment[1] for fragment in output[-1]) == "✓ read a.py\n"

    renderer.trace({"event": "model_requested"})
    assert "".join(fragment[1] for fragment in output[-1]) == "Working..."


@pytest.mark.parametrize(
    ("name", "args", "status", "error_code", "expected"),
    (
        (
            "run_shell",
            {"command": "private command"},
            "ok",
            "",
            "✓ run shell command\n",
        ),
        (
            "run_shell",
            {"command": "private command"},
            "error",
            "shell_failed",
            "× error · code=shell_failed · run shell command\n",
        ),
        (
            "read_tool_result",
            {"raw_result_id": "raw_private_id"},
            "partial_success",
            "tool_partial_success",
            "! partial_success · code=tool_partial_success · read tool result\n",
        ),
    ),
)
def test_tool_receipt_keeps_status_front_and_omits_raw_data(
    monkeypatch,
    name,
    args,
    status,
    error_code,
    expected,
):
    output = []
    monkeypatch.setattr("pony.tui.render.sys.stdout", io.StringIO())
    monkeypatch.setattr("pony.tui.render._terminal_columns", lambda: 80)
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )
    renderer = TuiRenderer(no_color=True)

    renderer.trace({"event": "tool_started", "name": name, "args": args})
    renderer.trace(
        {
            "event": "tool_executed",
            "tool_status": status,
            "tool_error_code": error_code,
            "result": "private raw result",
        }
    )

    receipt = "".join(fragment[1] for fragment in output[-1])
    assert receipt == expected
    for hidden in ("private command", "raw_private_id", "private raw result"):
        assert hidden not in receipt


@pytest.mark.parametrize(
    ("result_view", "expected"),
    (
        (
            {
                "delivery": "page",
                "truncated": False,
                "start_line": 1,
                "end_line": 2_000,
                "total_lines": 5_000,
                "next_start": 2_001,
                "reasons": ["lines"],
            },
            "✓ read big.txt · lines 1-2000/5000 · more from 2001 · limit=lines\n",
        ),
        (
            {
                "delivery": "page",
                "truncated": False,
                "start_line": 1,
                "end_line": 1,
                "total_lines": 1,
                "next_start_byte": 720,
                "reasons": ["bytes"],
            },
            "✓ read big.txt · lines 1-1/1 · more at byte 720 · limit=bytes\n",
        ),
        (
            {
                "delivery": "preview",
                "truncated": True,
                "reasons": ["tokens"],
                "recoverable": True,
            },
            "! run shell command · output truncated · limit=tokens · "
            "recoverable until this turn ends\n",
        ),
        (
            {
                "delivery": "preview",
                "truncated": True,
                "reasons": ["bytes"],
                "recoverable": False,
            },
            "! run shell command · output truncated · limit=bytes · not recoverable\n",
        ),
    ),
)
def test_tool_receipt_exposes_safe_page_and_recovery_state(
    monkeypatch,
    result_view,
    expected,
):
    output = []
    monkeypatch.setattr("pony.tui.render.sys.stdout", io.StringIO())
    monkeypatch.setattr("pony.tui.render._terminal_columns", lambda: 140)
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )
    renderer = TuiRenderer(no_color=True)
    name = "read_file" if result_view["delivery"] == "page" else "run_shell"
    args = {"path": "big.txt"} if name == "read_file" else {"command": "private"}

    renderer.trace({"event": "tool_started", "name": name, "args": args})
    renderer.trace(
        {
            "event": "tool_executed",
            "tool_status": "ok",
            "result_view": result_view,
        }
    )

    assert "".join(fragment[1] for fragment in output[-1]) == expected


def test_tool_receipt_fails_closed_on_malformed_result_view(monkeypatch):
    output = []
    monkeypatch.setattr("pony.tui.render.sys.stdout", io.StringIO())
    monkeypatch.setattr("pony.tui.render._terminal_columns", lambda: 120)
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )
    renderer = TuiRenderer(no_color=True)
    renderer.trace(
        {"event": "tool_started", "name": "read_file", "args": {"path": "a.py"}}
    )

    renderer.trace(
        {
            "event": "tool_executed",
            "tool_status": "ok",
            "result_view": {
                "delivery": "page",
                "truncated": False,
                "path": "private.txt",
            },
        }
    )

    assert "".join(fragment[1] for fragment in output[-1]) == (
        "! read a.py · result details unavailable\n"
    )


@pytest.mark.parametrize("columns", (10, 20, 80))
def test_tool_receipt_preserves_error_code_when_narrow(monkeypatch, columns):
    output = []
    monkeypatch.setattr("pony.tui.render.sys.stdout", io.StringIO())
    monkeypatch.setattr("pony.tui.render._terminal_columns", lambda: columns)
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )
    renderer = TuiRenderer(no_color=True)
    renderer.trace(
        {"event": "tool_started", "name": "run_shell", "args": {"command": "safe"}}
    )
    renderer.trace(
        {
            "event": "tool_executed",
            "tool_status": "error",
            "tool_error_code": "tool_failed",
            "result_view": {
                "delivery": "preview",
                "truncated": True,
                "reasons": ["bytes"],
                "recoverable": False,
            },
        }
    )

    rendered = "".join(fragment[1] for fragment in output[-1])
    compact = "".join(line.strip() for line in rendered.splitlines())
    assert "error" in compact
    assert "code=tool_failed" in compact
    assert all(get_cwidth(line) <= columns for line in rendered.splitlines())


def test_trace_projects_one_tool_line_and_hides_internal_lifecycle(monkeypatch):
    output = []
    terminal = io.StringIO()
    monkeypatch.setattr("pony.tui.render.sys.stdout", terminal)
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **kwargs: output.append((value, kwargs)),
    )
    renderer = TuiRenderer(no_color=True)

    renderer.trace({"event": "model_requested"})
    renderer.trace({"event": "model_requested"})
    assert len(output) == 1
    assert "Working..." in "".join(
        fragment[1] for fragment in output[0][0]
    )
    assert output[0][1]["end"] == ""

    renderer.trace(
        {
            "event": "tool_started",
            "name": "search",
            "args": {"pattern": "checkpoint", "path": "pony/"},
        }
    )
    after_start = len(output)
    assert "\r" in terminal.getvalue()
    renderer.trace({"event": "tool_finished", "tool_status": "ok"})
    renderer.trace({"event": "checkpoint_created", "checkpoint_id": "ckpt_hidden"})
    assert len(output) == after_start
    assert "Searching \"checkpoint\"..." in "".join(
        fragment[1] for fragment in output[-1][0]
    )

    renderer.trace(
        {
            "event": "tool_executed",
            "name": "search",
            "tool_status": "error",
            "tool_error_code": "permission_denied",
            "result": "permission denied",
        }
    )
    rendered = "".join(
        fragment[1] for value, _kwargs in output for fragment in value
    )
    assert "ckpt_hidden" not in rendered
    assert "× error · code=permission_denied" in rendered
    assert "permission denied" not in rendered


def test_user_block_keeps_a_plain_side_rail_and_hides_terminal_controls(monkeypatch):
    output = []
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )

    TuiRenderer(no_color=True).user("你好 **Pony**\x1b[31m", columns=20)

    rendered = "".join(fragment[1] for fragment in output[0])
    lines = rendered.splitlines()
    assert "\x1b" not in rendered
    assert len(lines) == 2
    assert lines[1].startswith("│ ")
    assert get_cwidth(lines[1]) == 19
    assert "你好 Pony[31m" in rendered
