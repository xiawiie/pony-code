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
            pass

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
        "pony.tui.render.shutil.get_terminal_size",
        lambda _fallback: SimpleNamespace(columns=current_columns["value"]),
    )
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: written.append(value),
    )

    assert run_tui(agent, model="gpt-test", no_color=True, handle_input=lambda *_a, **_k: 0) == 0
    assert [columns for columns, _text in samples] == [140, 80, 111, 112]
    for columns, rendered in samples:
        assert "⣿" in rendered and "█" in rendered
        if columns >= 112:
            assert all(get_cwidth(line) < columns for line in rendered.splitlines())
        assert "Ready" in rendered
        assert "Message Pony" in rendered
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
    agent = SimpleNamespace(
        _trace_listener=previous_listener,
        _approval_prompt=previous_prompt,
        current_permission_mode=lambda: "default",
        docker_sandbox=False,
        model_client=SimpleNamespace(provider="openai"),
        workspace=SimpleNamespace(cwd="/repo", branch="main"),
        session={"id": "session-id"},
    )

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def prompt(self, message, *_args, **_kwargs):
            if callable(message):
                output.append(message())
            return "/exit"

    monkeypatch.setattr("pony.tui.app._CompactPromptSession", FakeSession)
    monkeypatch.setattr(
        "pony.tui.render.shutil.get_terminal_size",
        lambda _fallback: SimpleNamespace(columns=120),
    )
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )

    def handle_input(received, text, **_kwargs):
        assert received._trace_listener is not previous_listener
        assert received._approval_prompt is not previous_prompt
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
    header = "".join(fragment[1] for fragment in output[0])
    assert "⣿" in header
    assert "█" in header
    assert "v1.0.0" in header
    assert "Local coding agent for repository-grounded work" in header
    assert "Ready · gpt-test · permission manual" in header


def test_tui_restores_runtime_hooks_when_provider_fails(monkeypatch):
    previous_listener = object()
    previous_prompt = object()
    agent = SimpleNamespace(
        _trace_listener=previous_listener,
        _approval_prompt=previous_prompt,
        current_permission_mode=lambda: "default",
        docker_sandbox=False,
        model_client=SimpleNamespace(provider="openai"),
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

        def prompt(self, *_args, **_kwargs):
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
        if "queued for next turn: 1/5 pending" in text:
            queued.set()

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


def test_busy_ctrl_c_clears_queue_but_current_turn_continues(monkeypatch):
    release = threading.Event()
    current_finished = threading.Event()
    notice_visible = threading.Event()
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
        if "request cancellation is unavailable" in text:
            notice_visible.set()

    def handle_input(_agent, text, **_kwargs):
        if text == "/exit":
            return 0
        calls.append(text)
        if text == "active":
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
    prompts.put(KeyboardInterrupt())
    assert notice_visible.wait(timeout=3)
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
    approval_finished = threading.Event()
    decisions = []
    prompt_threads = []
    worker_threads = []

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def prompt(self, *_args, **_kwargs):
            prompt_threads.append(threading.current_thread())
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
        if "APPROVAL REQUIRED" in text:
            approval_visible.set()

    def handle_input(received, text, **_kwargs):
        if text == "/exit":
            return 0
        worker_threads.append(threading.current_thread())
        assert text == "change file"
        decisions.append(received._approval_prompt("write_file", {"path": "a.txt"}))
        approval_finished.set()

    monkeypatch.setattr("pony.tui.app._CompactPromptSession", FakeSession)
    monkeypatch.setattr("pony.tui.render.print_formatted_text", write)
    outcome = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            run_tui(agent, model="gpt-test", no_color=True, handle_input=handle_input)
        )
    )
    thread.start()

    inputs.put("change file")
    assert approval_visible.wait(timeout=3)
    inputs.put("yes")
    assert approval_finished.wait(timeout=3)
    inputs.put("/exit")
    thread.join(timeout=3)

    assert outcome == [0]
    assert decisions == [True]
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


def test_toolbar_projects_trace_state_queue_and_existing_context_metadata(monkeypatch):
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

    def footer(*, busy=False, pending=0):
        rendered = renderer.toolbar(
            agent,
            model="gpt-test",
            columns=140,
            busy=busy,
            pending=pending,
        )
        return "".join(fragment[1] for fragment in rendered).splitlines()[-1]

    renderer.trace({"event": "run_started"})
    assert "Preparing" in footer(busy=True, pending=1)
    assert "queue 1/5" in footer(busy=True, pending=1)

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
    assert "Waiting for model" in footer(busy=True)
    assert "ctx 25%" in footer(busy=True)

    renderer.trace(
        {"event": "model_requested", "attempt_origin": "model_retry"}
    )
    assert "Retrying" in footer(busy=True)

    renderer.trace(
        {"event": "tool_started", "name": "read_file", "args": {"path": "a.py"}}
    )
    assert "Using tool" in footer(busy=True)
    renderer.trace({"event": "tool_executed", "tool_status": "ok"})

    renderer.trace({"event": "context_compacted"})
    assert "Compacted" in footer(busy=True)
    renderer.trace(
        {
            "event": "run_finished",
            "status": "completed",
            "stop_reason": "final_answer_returned",
        }
    )
    assert "Completed" in footer()

    renderer.notice("local command failed", error=True)
    assert "Completed" in footer()

    interrupted = project_trace_event(
        task_state,
        "model_failed",
        {"outcome": "interrupted"},
        created_at="now",
    )
    assert interrupted["outcome"] == "interrupted"
    renderer.trace(interrupted)
    assert "Interrupted" in footer()
    renderer.trace(
        {
            "event": "run_finished",
            "status": "stopped",
            "stop_reason": "interrupted",
        }
    )
    assert "Interrupted" in footer()


@pytest.mark.parametrize(
    ("terminal_event", "expected"),
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
def test_answer_does_not_override_durable_terminal_state(
    monkeypatch,
    terminal_event,
    expected,
):
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
    )
    renderer = TuiRenderer(no_color=True)

    renderer.trace(terminal_event)
    renderer.answer("Runtime terminal message", columns=80)

    footer = "".join(
        fragment[1]
        for fragment in renderer.toolbar(agent, model="gpt-test", columns=112)
    ).splitlines()[-1]
    assert expected in footer


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
        "pony.tui.render.shutil.get_terminal_size",
        lambda _fallback: SimpleNamespace(columns=columns),
    )
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **kwargs: output.append((value, kwargs)),
    )
    renderer = TuiRenderer(no_color=True)

    renderer.trace({"event": "tool_started", "name": name, "args": args})

    activity = "".join(fragment[1] for fragment in output[0][0])
    assert activity.startswith("PONY  Using tool · ")
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
        "pony.tui.render.shutil.get_terminal_size",
        lambda _fallback: current,
    )
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda *_args, **_kwargs: None,
    )
    renderer = TuiRenderer(no_color=True)
    renderer.trace(
        {
            "event": "tool_started",
            "name": "run_shell",
            "args": {"command": "pytest " + "long-test " * 40},
        }
    )

    current.columns = 80
    renderer.trace({"event": "tool_executed", "tool_status": "ok"})

    cleared = terminal.getvalue().split("\r")[1]
    assert get_cwidth(cleared) == 79


@pytest.mark.parametrize("columns", (80, 112))
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


def test_approval_resolution_replaces_waiting_state(monkeypatch):
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
    )
    renderer = TuiRenderer(no_color=True)
    renderer.trace(
        {"event": "tool_started", "name": "write_file", "args": {"path": "a.py"}}
    )
    renderer.approval("write_file", {"path": "a.py"}, columns=80)

    renderer.approval_resolved(True)

    footer = "".join(
        fragment[1]
        for fragment in renderer.toolbar(agent, model="gpt-test", columns=112)
    )
    assert "Using tool" in footer
    assert "Waiting for approval" not in footer


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
    assert "PONY\n  The failure is isolated." in rendered
    assert "Message Pony\n› " in prompt


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
    assert "PONY  Waiting for model..." in "".join(
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
    assert "PONY  Using tool · search \"checkpoint\" in pony/..." in "".join(
        fragment[1] for fragment in output[-1][0]
    )

    renderer.trace(
        {
            "event": "tool_executed",
            "name": "search",
            "tool_status": "error",
            "result": "permission denied",
        }
    )
    rendered = "".join(
        fragment[1] for value, _kwargs in output for fragment in value
    )
    assert "ckpt_hidden" not in rendered
    assert "permission denied" in rendered


def test_user_block_has_padding_without_exposing_terminal_controls(monkeypatch):
    output = []
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )

    TuiRenderer(no_color=False).user("你好 **Pony**\x1b[31m", columns=20)

    rendered = "".join(fragment[1] for fragment in output[0])
    lines = rendered.splitlines()
    assert "\x1b" not in rendered
    assert len(lines) == 4
    assert all(get_cwidth(line) == 19 for line in lines[1:])
    assert "你好 Pony[31m" in rendered
