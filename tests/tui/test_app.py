import io
from types import SimpleNamespace

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.utils import get_cwidth

from pony.cli.start import run_repl
from pony.providers.transport import ProviderTransportError
from pony.tui.app import (
    SlashCommandCompleter,
    _CompactPromptSession,
    _key_bindings,
    _history,
    run_tui,
    should_use_tui,
)
from pony.tui.render import _COLOR_STYLE, _PLAIN_STYLE, TuiRenderer


class _Stream:
    def __init__(self, is_tty):
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty", "term", "columns", "expected"),
    (
        (True, True, "xterm-256color", 80, True),
        (False, True, "xterm-256color", 80, False),
        (True, False, "xterm-256color", 80, False),
        (True, True, "dumb", 80, False),
        (True, True, "xterm-256color", 39, False),
    ),
)
def test_tui_requires_a_capable_interactive_terminal(
    stdin_tty,
    stdout_tty,
    term,
    columns,
    expected,
):
    assert should_use_tui(
        stdin=_Stream(stdin_tty),
        stdout=_Stream(stdout_tty),
        environ={"TERM": term},
        columns=columns,
    ) is expected


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
    assert {"/mode", "/plan"} <= {
        item.text
        for item in SlashCommandCompleter().get_completions(Document("/"), None)
    }


def test_tui_history_uses_only_supplied_canonical_prompts():
    assert _history(["first", "active branch"]).get_strings() == [
        "first",
        "active branch",
    ]


def test_tui_resume_card_labels_fact_sources(monkeypatch):
    output = []
    monkeypatch.setattr(
        "pony.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )
    projection = {
        "mode": "plan",
        "goal": {"text": "Ship", "source": "plan"},
        "plan": {"completed_count": 1, "item_count": 2, "current_count": 1},
        "checkpoint": {"status": "ready", "blocker": "", "next_steps": []},
        "resume": {"status": "ready"},
    }

    TuiRenderer(no_color=True).resume(projection)

    rendered = "".join(fragment[1] for fragment in output[0])
    assert "mode [session]: plan" in rendered
    assert "goal [plan]: Ship" in rendered
    assert "checkpoint [checkpoint]: status=ready" in rendered


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


def test_repl_routes_a_capable_tty_to_tui_and_finalizes(monkeypatch):
    calls = []
    agent = SimpleNamespace(finalize_sandbox_session=lambda: calls.append("finalize"))
    monkeypatch.setattr("pony.tui.app.should_use_tui", lambda: True)

    def fake_run_tui(received, **options):
        assert received is agent
        assert callable(options["handle_input"])
        calls.append(
            (options["model"], options["no_color"], options["show_header"])
        )
        return 0

    monkeypatch.setattr("pony.tui.app.run_tui", fake_run_tui)

    assert run_repl(agent, model="model", no_color=True) == 0
    assert calls == [("model", True, True), "finalize"]


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
        approval_policy="ask",
        docker_sandbox=False,
        model_client=SimpleNamespace(provider="openai"),
        workspace=SimpleNamespace(cwd="/repo", branch="main"),
        session={"id": "session-id"},
    )

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def prompt(self, *_args, **_kwargs):
            return "/exit"

    monkeypatch.setattr("pony.tui.app._CompactPromptSession", FakeSession)
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
    assert header == "PONY CODE · v1.0.0\n"


def test_tui_restores_runtime_hooks_when_provider_fails(monkeypatch):
    previous_listener = object()
    previous_prompt = object()
    agent = SimpleNamespace(
        _trace_listener=previous_listener,
        _approval_prompt=previous_prompt,
        approval_policy="ask",
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


def test_toolbar_is_width_bounded_and_keeps_only_essential_status():
    agent = SimpleNamespace(
        approval_policy="ask",
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

    for columns in (40, 80, 120):
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
        assert "host" in footer
        assert "act/ask" in footer
        if columns >= 80:
            assert "project" in footer
            assert "anthropic/claude-sonnet-4-6" in footer
        if columns == 120:
            assert "feature/very-long-branch" in footer
        assert "/very/long" not in rendered
        assert "session-must-not-appear" not in rendered
        assert "checkpoint-must-not-appear" not in rendered
        assert "api-must-not-appear" not in rendered


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
    assert "Working…" in "".join(fragment[1] for fragment in output[0][0])
    assert output[0][1]["end"] == ""

    renderer.trace(
        {
            "event": "tool_started",
            "name": "search",
            "args": {"pattern": "checkpoint", "path": "pony/"},
        }
    )
    after_start = len(output)
    assert terminal.getvalue() == "\r        \r"
    renderer.trace({"event": "tool_finished", "tool_status": "ok"})
    renderer.trace({"event": "checkpoint_created", "checkpoint_id": "ckpt_hidden"})
    assert len(output) == after_start
    assert "› search \"checkpoint\" in pony/" in "".join(
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
    assert len(lines) == 4  # outside spacing + top padding + content + bottom padding
    assert all(get_cwidth(line) == 19 for line in lines[1:])
