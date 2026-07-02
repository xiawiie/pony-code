import json
import os

from pico.cli import main


def _clear_provider_env(monkeypatch):
    for name in (
        "PICO_PROVIDER",
        "PICO_DEEPSEEK_MODEL",
        "DEEPSEEK_MODEL",
        "PICO_DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_status_json_reports_storage_without_building_agent(tmp_path, monkeypatch, capsys):
    _clear_provider_env(monkeypatch)
    (tmp_path / ".pico" / "sessions").mkdir(parents=True)
    (tmp_path / ".pico" / "runs" / "run_1").mkdir(parents=True)
    (tmp_path / ".pico" / "checkpoints" / "records").mkdir(parents=True)

    def fail_build_agent(args):
        raise AssertionError("status must not build a Pico agent")

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)

    code = main(["--cwd", str(tmp_path), "--format", "json", "status"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "status"
    assert payload["data"]["storage"]["sessions"] is True
    assert payload["data"]["storage"]["runs"] is True
    assert payload["data"]["storage"]["checkpoints"] is True
    assert payload["data"]["latest"]["run_id"] == "run_1"
    assert "memory" not in payload["data"]


def test_config_show_json_reports_sources_without_secret_values(tmp_path, monkeypatch, capsys):
    _clear_provider_env(monkeypatch)
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=deepseek\nPICO_DEEPSEEK_API_KEY=secret-value\n",
        encoding="utf-8",
    )

    def fail_build_agent(args):
        raise AssertionError("config show must not build a Pico agent")

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)

    code = main(["--cwd", str(tmp_path), "--format", "json", "config", "show"])

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["kind"] == "config_show"
    assert payload["data"]["provider"] == {
        "value": "deepseek",
        "source": "project_env",
        "name": "PICO_PROVIDER",
    }
    assert payload["data"]["api_key"] == {
        "present": True,
        "source": "project_env",
        "name": "PICO_DEEPSEEK_API_KEY",
    }
    assert "secret-value" not in captured.out


def test_config_show_does_not_mutate_environment(tmp_path, monkeypatch, capsys):
    _clear_provider_env(monkeypatch)
    (tmp_path / ".env").write_text(
        "PICO_DEEPSEEK_API_KEY=secret-value\n",
        encoding="utf-8",
    )

    code = main(["--cwd", str(tmp_path), "--format", "json", "config", "show"])

    captured = capsys.readouterr()
    assert code == 0
    assert "PICO_DEEPSEEK_API_KEY" not in os.environ
    assert "secret-value" not in captured.out


def test_doctor_offline_skips_connectivity(tmp_path, monkeypatch, capsys):
    called = {}

    def fake_connectivity(config):
        called["connectivity"] = True
        return {"status": "ok"}

    monkeypatch.setattr("pico.cli_diagnostics.check_provider_connectivity", fake_connectivity)

    code = main(["--cwd", str(tmp_path), "--format", "json", "doctor", "--offline"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "doctor"
    assert called == {}
    assert payload["data"]["provider_connectivity"]["status"] == "skipped"


def test_doctor_reports_connectivity_as_diagnostic_result(tmp_path, monkeypatch, capsys):
    def fake_connectivity(config):
        return {
            "status": "error",
            "category": "provider_connectivity",
            "message": "connection timed out",
        }

    monkeypatch.setattr("pico.cli_diagnostics.check_provider_connectivity", fake_connectivity)

    code = main(["--cwd", str(tmp_path), "--format", "json", "doctor"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["provider_connectivity"]["category"] == "provider_connectivity"
    assert payload["data"]["provider_connectivity"]["message"] == "connection timed out"


def test_sessions_list_and_show_json_do_not_build_agent(tmp_path, monkeypatch, capsys):
    session_dir = tmp_path / ".pico" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / "session_1.json").write_text('{"id": "session_1", "history": []}\n', encoding="utf-8")

    def fail_build_agent(args):
        raise AssertionError("sessions must not build a Pico agent")

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)

    code = main(["--cwd", str(tmp_path), "--format", "json", "sessions", "list"])
    assert code == 0
    list_payload = json.loads(capsys.readouterr().out)
    assert list_payload == {"ok": True, "kind": "sessions_list", "data": [{"session_id": "session_1"}]}

    code = main(["--cwd", str(tmp_path), "--format", "json", "sessions", "show", "session_1"])
    assert code == 0
    show_payload = json.loads(capsys.readouterr().out)
    assert show_payload["kind"] == "sessions_show"
    assert show_payload["data"]["id"] == "session_1"


def test_sessions_show_rejects_path_escape(tmp_path, capsys):
    session_dir = tmp_path / ".pico" / "sessions"
    session_dir.mkdir(parents=True)
    (tmp_path / "outside.json").write_text('{"id": "outside", "secret": "leaked"}\n', encoding="utf-8")

    code = main(["--cwd", str(tmp_path), "--format", "json", "sessions", "show", "../../outside"])

    captured = capsys.readouterr()
    assert code == 2
    assert "leaked" not in captured.out
    assert "leaked" not in captured.err
