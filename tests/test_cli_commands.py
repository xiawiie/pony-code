import json

import pytest

from pico.cli import main


def _install_fake_agent(monkeypatch, tmp_path, called):
    def fake_build_agent(args):
        called["built"] = True
        called["prompt"] = list(getattr(args, "prompt", []))

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            approval_policy = "auto"
            session = {"id": "s"}
            session_path = str(tmp_path / ".pico" / "sessions" / "s.json")

            def ask(self, message):
                called["asked"] = message
                return "answer"

            def memory_text(self):
                return "memory"

            def reset(self):
                called["reset"] = True

        return FakeAgent()

    monkeypatch.setattr("pico.cli.build_agent", fake_build_agent)
    monkeypatch.setattr("pico.cli.build_welcome", lambda agent, model, host: "")


def test_run_command_calls_agent_once(tmp_path, monkeypatch, capsys):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    code = main(["--cwd", str(tmp_path), "run", "fix", "tests"])

    assert code == 0
    assert called["asked"] == "fix tests"
    assert "answer" in capsys.readouterr().out


def test_repl_command_exits_on_eof(tmp_path, monkeypatch):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError()))

    code = main(["--cwd", str(tmp_path), "repl"])

    assert code == 0
    assert called["built"] is True


def test_legacy_prompt_remains_silent_compatibility(tmp_path, monkeypatch, capsys):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    code = main(["--cwd", str(tmp_path), "fix", "tests"])

    assert code == 0
    out = capsys.readouterr().out
    assert "answer" in out
    assert "pico run" not in out


def test_help_command_shows_examples(capsys):
    code = main(["help"])

    assert code == 0
    out = capsys.readouterr().out
    assert 'pico run "inspect the failing tests"' in out
    assert "Diagnostics:" in out
    assert "providers list" not in out


def test_unknown_command_suggests_close_match(capsys):
    code = main(["chekpoints", "list"])

    assert code == 2
    err = capsys.readouterr().err
    assert "Unknown command: chekpoints" in err
    assert "Did you mean `checkpoints`?" in err


@pytest.mark.parametrize(
    ("tokens", "prompt"),
    [
        (["hello"], "hello"),
        (["start", "a", "project"], "start a project"),
        (["running", "tests"], "running tests"),
        (["check", "tests"], "check tests"),
    ],
)
def test_natural_language_legacy_prompts_are_not_command_typos(
    tmp_path,
    monkeypatch,
    tokens,
    prompt,
):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    code = main(["--cwd", str(tmp_path), *tokens])

    assert code == 0
    assert called["asked"] == prompt


def test_unknown_command_suggestion_uses_json_error_envelope(capsys):
    code = main(["--format", "json", "chekpoints", "list"])

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unknown_command"
    assert payload["error"]["message"] == "Unknown command: chekpoints"
    assert payload["error"]["hint"] == "Did you mean `checkpoints`?"
