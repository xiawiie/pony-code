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


def test_no_input_blocks_repl_before_input(tmp_path, monkeypatch, capsys):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(AssertionError("input called")))

    code = main(["--cwd", str(tmp_path), "--no-input", "repl"])

    assert code == 2
    assert "--no-input" in capsys.readouterr().err


def test_quiet_suppresses_welcome_for_run(tmp_path, monkeypatch, capsys):
    called = {}

    def fake_build_agent(args):
        called["built"] = True

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            approval_policy = "auto"
            session = {"id": "s"}

            def ask(self, message):
                called["asked"] = message
                return "answer"

        return FakeAgent()

    monkeypatch.setattr("pico.cli.build_agent", fake_build_agent)
    monkeypatch.setattr("pico.cli.build_welcome", lambda agent, model, host: "WELCOME")

    code = main(["--cwd", str(tmp_path), "--quiet", "run", "fix"])

    assert code == 0
    out = capsys.readouterr().out
    assert "answer" in out
    assert "WELCOME" not in out


def test_legacy_prompt_remains_silent_compatibility(tmp_path, monkeypatch, capsys):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    code = main(["--cwd", str(tmp_path), "fix", "tests"])

    assert code == 0
    out = capsys.readouterr().out
    assert "answer" in out
    assert "pico run" not in out
