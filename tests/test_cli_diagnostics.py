import json
import os
from urllib import error

from pico.cli import main
from pico.cli_diagnostics import check_provider_connectivity


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


def test_doctor_json_does_not_build_agent(tmp_path, monkeypatch, capsys):
    def fail_build_agent(args):
        raise AssertionError("doctor must not build a Pico agent")

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)

    code = main(["--cwd", str(tmp_path), "--format", "json", "doctor", "--offline"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "doctor"


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


def test_doctor_json_redacts_secret_base_url(tmp_path, monkeypatch, capsys):
    def fake_connectivity(config):
        return {
            "status": "error",
            "category": "provider_connectivity",
            "message": "offline test double",
        }

    monkeypatch.setattr("pico.cli_diagnostics.check_provider_connectivity", fake_connectivity)

    code = main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "--base-url",
        "https://user:pass@example.com/v1?token=secret#frag",
        "doctor",
    ])

    assert code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["kind"] == "doctor"
    assert payload["data"]["config"]["base_url"]["value"] == "https://example.com/v1"
    assert "user:pass" not in output
    assert "token=" not in output
    assert "secret" not in output
    assert "#frag" not in output


def test_provider_connectivity_http_500_is_non_ok_and_redacts_url(monkeypatch):
    def fake_urlopen(url, timeout):
        raise error.HTTPError(
            url,
            500,
            "server error",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("pico.cli_diagnostics.request.urlopen", fake_urlopen)

    result = check_provider_connectivity(
        {
            "provider": {"value": "openai"},
            "base_url": {"value": "https://user:pass@example.com/v1?token=secret#frag"},
        }
    )

    assert result["status"] != "ok"
    assert result["url"] == "https://example.com/v1"
    assert "user:pass" not in json.dumps(result)
    assert "token=" not in json.dumps(result)
    assert "secret" not in json.dumps(result)
    assert "#frag" not in json.dumps(result)


def test_doctor_unknown_arg_returns_usage_without_agent_or_connectivity(tmp_path, monkeypatch, capsys):
    called = {}

    def fail_build_agent(args):
        raise AssertionError("doctor usage errors must not build a Pico agent")

    def fake_connectivity(config):
        called["connectivity"] = True
        return {"status": "ok"}

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)
    monkeypatch.setattr("pico.cli_diagnostics.check_provider_connectivity", fake_connectivity)

    code = main(["--cwd", str(tmp_path), "--format", "json", "doctor", "--wat"])

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage"
    assert called == {}


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
