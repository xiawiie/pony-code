import json
import os
from urllib import error

from pico.cli import main
from pico.cli_diagnostics import check_model_connectivity


def _clear_model_env(monkeypatch):
    for name in (
        "DEEPSEEK_API_KEY",
        "MODEL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _write_model_config(
    tmp_path,
    *,
    name="deepseek-chat",
    base_url="https://api.deepseek.com/anthropic",
    api_key_env="DEEPSEEK_API_KEY",
    api="anthropic-messages",
):
    lines = [
        "[model]",
        f'name = "{name}"',
        f'base_url = "{base_url}"',
    ]
    if api_key_env:
        lines.append(f'api_key_env = "{api_key_env}"')
    if api:
        lines.append(f'api = "{api}"')
    (tmp_path / "pico.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_status_json_reports_storage_without_building_agent(tmp_path, monkeypatch, capsys):
    _clear_model_env(monkeypatch)
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
    assert payload["data"]["model"]["status"] == "ok"
    assert "memory" not in payload["data"]


def test_status_text_uses_grouped_cli_output(tmp_path, monkeypatch, capsys):
    _clear_model_env(monkeypatch)
    (tmp_path / ".pico" / "sessions").mkdir(parents=True)
    (tmp_path / ".pico" / "runs" / "run_1").mkdir(parents=True)

    code = main(["--cwd", str(tmp_path), "status"])

    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("Pico status — Local harness state\n")
    assert "Workspace" in out
    assert "Model" in out
    assert "Provider" not in out
    assert "Storage" in out
    assert not out.lstrip().startswith("{")


def test_config_show_json_reports_model_without_secret_values(tmp_path, monkeypatch, capsys):
    _clear_model_env(monkeypatch)
    _write_model_config(tmp_path)
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=secret-value\n",
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
    assert payload["data"]["model"] == {
        "status": "ok",
        "name": "deepseek-chat",
        "base_url": "https://api.deepseek.com/anthropic",
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key_present": True,
        "api": "anthropic-messages",
        "adapter": "AnthropicMessagesAdapter",
        "native_tools": True,
        "prompt_cache": False,
    }
    assert "secret-value" not in captured.out


def test_config_show_skips_malformed_project_env_lines_with_warning(tmp_path, monkeypatch, capsys):
    _clear_model_env(monkeypatch)
    _write_model_config(tmp_path)
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=secret-value\n"
        "not a valid env line\n",
        encoding="utf-8",
    )

    code = main(["--cwd", str(tmp_path), "--format", "json", "config", "show"])

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["data"]["model"]["status"] == "ok"
    assert payload["data"]["model"]["api_key_present"] is True
    assert "warning: skipped invalid .env line 2" in captured.err
    assert "secret-value" not in captured.out


def test_config_show_text_uses_grouped_cli_output_without_secret_value(tmp_path, monkeypatch, capsys):
    _clear_model_env(monkeypatch)
    _write_model_config(tmp_path)
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=secret-value\n",
        encoding="utf-8",
    )

    code = main(["--cwd", str(tmp_path), "config", "show"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.startswith("Pico config — Effective configuration\n")
    assert "Model" in captured.out
    assert "Provider" not in captured.out
    assert "present" in captured.out
    assert "secret-value" not in captured.out
    assert not captured.out.lstrip().startswith("{")


def test_config_show_does_not_mutate_environment(tmp_path, monkeypatch, capsys):
    _clear_model_env(monkeypatch)
    _write_model_config(tmp_path)
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=secret-value\n",
        encoding="utf-8",
    )

    code = main(["--cwd", str(tmp_path), "--format", "json", "config", "show"])

    captured = capsys.readouterr()
    assert code == 0
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert "secret-value" not in captured.out


def test_doctor_offline_skips_connectivity(tmp_path, monkeypatch, capsys):
    called = {}

    def fake_connectivity(config):
        called["connectivity"] = True
        return {"status": "ok"}

    monkeypatch.setattr("pico.cli_diagnostics.check_model_connectivity", fake_connectivity)

    code = main(["--cwd", str(tmp_path), "--format", "json", "doctor", "--offline"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "doctor"
    assert called == {}
    assert payload["data"]["model_connectivity"]["status"] == "skipped"


def test_doctor_text_uses_grouped_cli_output(tmp_path, monkeypatch, capsys):
    code = main(["--cwd", str(tmp_path), "doctor", "--offline"])

    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("Pico doctor — CLI health check\n")
    assert "Workspace" in out
    assert "Config" in out
    assert "Model" in out
    assert "Storage" in out
    assert "Model connectivity" in out
    assert "skipped" in out
    assert not out.lstrip().startswith("{")


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
            "category": "model_connectivity",
            "message": "connection timed out",
        }

    monkeypatch.setattr("pico.cli_diagnostics.check_model_connectivity", fake_connectivity)

    code = main(["--cwd", str(tmp_path), "--format", "json", "doctor"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["model_connectivity"]["category"] == "model_connectivity"
    assert payload["data"]["model_connectivity"]["message"] == "connection timed out"


def test_doctor_json_redacts_secret_base_url(tmp_path, monkeypatch, capsys):
    _write_model_config(
        tmp_path,
        name="custom-model",
        base_url="https://user:pass@example.com/v1?token=secret#frag",
        api_key_env="",
        api="openai-chat",
    )

    def fake_connectivity(config):
        return {
            "status": "error",
            "category": "model_connectivity",
            "message": "offline test double",
        }

    monkeypatch.setattr("pico.cli_diagnostics.check_model_connectivity", fake_connectivity)

    code = main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "doctor",
    ])

    assert code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["kind"] == "doctor"
    assert payload["data"]["config"]["model"]["base_url"] == "https://example.com/v1"
    assert "user:pass" not in output
    assert "token=" not in output
    assert "secret" not in output
    assert "#frag" not in output


def test_model_connectivity_http_500_is_non_ok_and_redacts_url(monkeypatch):
    def fake_urlopen(url, timeout):
        raise error.HTTPError(
            url,
            500,
            "server error",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("pico.cli_diagnostics.request.urlopen", fake_urlopen)

    result = check_model_connectivity(
        {
            "model": {
                "api": "openai-chat",
                "base_url": "https://user:pass@example.com/v1?token=secret#frag",
            },
        }
    )

    assert result["status"] != "ok"
    assert result["url"] == "https://example.com/v1"
    assert "user:pass" not in json.dumps(result)
    assert "token=" not in json.dumps(result)
    assert "secret" not in json.dumps(result)
    assert "#frag" not in json.dumps(result)


def test_model_connectivity_generic_error_sanitizes_url_in_message(monkeypatch):
    def fake_urlopen(url, timeout):
        raise RuntimeError(f"failed to open {url}")

    monkeypatch.setattr("pico.cli_diagnostics.request.urlopen", fake_urlopen)

    result = check_model_connectivity(
        {
            "model": {
                "api": "openai-chat",
                "base_url": "https://user:pass@example.com/v1?token=secret#frag",
            },
        }
    )

    output = json.dumps(result)
    assert result["status"] == "error"
    assert result["url"] == "https://example.com/v1"
    assert "RuntimeError" in result["message"]
    assert "user:pass" not in output
    assert "token=" not in output
    assert "secret" not in output
    assert "#frag" not in output


def test_doctor_unknown_arg_returns_usage_without_agent_or_connectivity(tmp_path, monkeypatch, capsys):
    called = {}

    def fail_build_agent(args):
        raise AssertionError("doctor usage errors must not build a Pico agent")

    def fake_connectivity(config):
        called["connectivity"] = True
        return {"status": "ok"}

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)
    monkeypatch.setattr("pico.cli_diagnostics.check_model_connectivity", fake_connectivity)

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
