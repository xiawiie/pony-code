from types import SimpleNamespace

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.utils import get_cwidth

from pico.cli.start import run_repl
from pico.tui.app import (
    SlashCommandCompleter,
    _CompactPromptSession,
    _key_bindings,
    run_tui,
    should_use_tui,
)
from pico.tui.render import _COLOR_STYLE, logo_text


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


@pytest.mark.parametrize(("columns", "height"), ((40, 5), (80, 7), (120, 11)))
def test_terminal_logo_scales_horse_and_wordmark_together(columns, height):
    rendered = logo_text(columns)
    lines = rendered.splitlines()

    assert "HERMES" not in rendered
    assert "⣿" in rendered
    assert "█" in rendered
    assert "\x1b" not in rendered
    assert len(lines) == height
    assert max(get_cwidth(line) for line in lines) < columns


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
    assert rules["success"] == "#3fb950"


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
        reserve_space_for_menu=10,
    )

    assert _CompactPromptSession._get_default_buffer_control_height(session).max == 1
    session.default_buffer.document.line_count = 9
    assert _CompactPromptSession._get_default_buffer_control_height(session).max == 6


def test_repl_routes_a_capable_tty_to_tui_and_finalizes(monkeypatch):
    calls = []
    agent = SimpleNamespace(finalize_sandbox_session=lambda: calls.append("finalize"))
    monkeypatch.setattr("pico.tui.app.should_use_tui", lambda: True)

    def fake_run_tui(received, **options):
        assert received is agent
        assert callable(options["handle_input"])
        calls.append(
            (options["model"], options["no_color"], options["show_header"])
        )
        return 0

    monkeypatch.setattr("pico.tui.app.run_tui", fake_run_tui)

    assert run_repl(agent, model="model", no_color=True) == 0
    assert calls == [("model", True, True), "finalize"]


def test_plain_repl_never_starts_tui(monkeypatch):
    agent = SimpleNamespace()
    monkeypatch.setattr(
        "pico.tui.app.run_tui",
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

    monkeypatch.setattr("pico.tui.app._CompactPromptSession", FakeSession)
    monkeypatch.setattr(
        "pico.tui.render.print_formatted_text",
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
    assert "v1.0.0" in header
    assert "HERMES" not in header
    assert "Local coding agent for repository-grounded work" in header
    assert "Using gpt-test · approval ask" in header


def test_compact_header_keeps_version_model_and_intro_within_terminal(
    monkeypatch,
):
    output = []
    agent = SimpleNamespace(
        approval_policy="ask",
        model_client=SimpleNamespace(
            provider_metadata={"protocol_family": "anthropic_messages"}
        ),
    )
    monkeypatch.setattr("pico.tui.render.metadata.version", lambda _name: "1.2.3")
    monkeypatch.setattr(
        "pico.tui.render.print_formatted_text",
        lambda value, **_kwargs: output.append(value),
    )

    from pico.tui.render import TuiRenderer

    TuiRenderer(no_color=True).header(
        agent,
        model="claude-sonnet-4-6",
        columns=40,
    )

    header = "".join(fragment[1] for fragment in output[0])
    assert "v1.2.3" in header
    assert any(line.strip() == "v1.2.3" for line in header.splitlines())
    assert "Repository-grounded coding agent" in header
    assert "Using anthropic/claude-sonnet-4-6" in header
    assert all(get_cwidth(line) < 40 for line in header.splitlines())
