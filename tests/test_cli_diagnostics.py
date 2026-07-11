import json
import os
from unittest.mock import Mock
from urllib import error

import pico.cli_diagnostics as cli_diagnostics_module
from pico.cli import main
from pico.cli_diagnostics import check_provider_connectivity, collect_doctor


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


def test_status_text_uses_grouped_cli_output(tmp_path, monkeypatch, capsys):
    _clear_provider_env(monkeypatch)
    (tmp_path / ".pico" / "sessions").mkdir(parents=True)
    (tmp_path / ".pico" / "runs" / "run_1").mkdir(parents=True)

    code = main(["--cwd", str(tmp_path), "status"])

    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("Pico status — Local harness state\n")
    assert "Workspace" in out
    assert "Provider" in out
    assert "Storage" in out
    assert not out.lstrip().startswith("{")


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


def test_config_show_skips_malformed_project_env_lines_with_warning(tmp_path, monkeypatch, capsys):
    _clear_provider_env(monkeypatch)
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=deepseek\n"
        "not a valid env line\n"
        "PICO_DEEPSEEK_API_KEY=secret-value\n",
        encoding="utf-8",
    )

    code = main(["--cwd", str(tmp_path), "--format", "json", "config", "show"])

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["data"]["provider"]["value"] == "deepseek"
    assert payload["data"]["api_key"]["present"] is True
    assert "warning: skipped invalid .env line 2" in captured.err
    assert "secret-value" not in captured.out


def test_config_show_text_uses_grouped_cli_output_without_secret_value(tmp_path, monkeypatch, capsys):
    _clear_provider_env(monkeypatch)
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=deepseek\nPICO_DEEPSEEK_API_KEY=secret-value\n",
        encoding="utf-8",
    )

    code = main(["--cwd", str(tmp_path), "config", "show"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.startswith("Pico config — Effective configuration\n")
    assert "Provider" in captured.out
    assert "Credentials" in captured.out
    assert "present" in captured.out
    assert "secret-value" not in captured.out
    assert not captured.out.lstrip().startswith("{")


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


def test_doctor_security_metadata_has_only_safe_status_modes_and_missing_names(
    tmp_path,
    monkeypatch,
):
    secret = "ghp_" + "D" * 32
    monkeypatch.setenv("PICO_TEST_SECRET", secret)
    (tmp_path / ".env").write_text(
        f"PICO_TEST_SECRET={secret}\n",
        encoding="utf-8",
    )
    (tmp_path / ".pico").mkdir()

    security = collect_doctor(tmp_path, offline=True)["security"]

    assert set(security) == {
        "status",
        "project_env",
        "private_storage",
        "trusted_executables",
    }
    assert set(security["project_env"]) == {"status", "mode"}
    assert set(security["private_storage"]) == {"status"}
    assert set(security["trusted_executables"]) == {"status", "missing"}
    assert all(
        name and "/" not in name and "\\" not in name
        for name in security["trusted_executables"]["missing"]
    )
    assert secret not in json.dumps(security)
    assert str(tmp_path) not in json.dumps(security)
    if os.name == "posix":
        assert security["project_env"] == {
            "status": "ok",
            "mode": "0600",
        }
        assert security["private_storage"] == {"status": "review_required"}
        assert security["status"] == "review_required"


def test_doctor_security_folds_unsafe_storage_and_executable_paths_to_status(
    tmp_path,
    monkeypatch,
    capsys,
):
    secret = "ghp_" + "S" * 32
    monkeypatch.setenv("PICO_TEST_SECRET", secret)
    outside = tmp_path / ("outside-" + secret)
    outside.write_text(secret, encoding="utf-8")
    (tmp_path / ".env").symlink_to(outside)
    private_root = tmp_path / ".pico"
    private_root.mkdir(mode=0o700)
    (private_root / "linked").symlink_to(outside)

    original_build = cli_diagnostics_module.WorkspaceContext.build

    def build_workspace(cwd):
        workspace = original_build(cwd)
        workspace.trusted_executables = {
            "git": f"/{secret}/git",
            "untrusted": f"/{secret}/evil",
        }
        return workspace

    monkeypatch.setattr(
        "pico.cli_diagnostics.WorkspaceContext.build",
        build_workspace,
    )

    security = collect_doctor(tmp_path, offline=True)["security"]

    assert security == {
        "status": "review_required",
        "project_env": {"status": "review_required", "mode": ""},
        "private_storage": {"status": "review_required"},
        "trusted_executables": {"status": "degraded", "missing": ["rg"]},
    }
    assert secret not in json.dumps(security)

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "doctor",
        "--offline",
    ]) == 0
    output = capsys.readouterr().out
    assert secret not in output


def test_doctor_security_folds_executable_discovery_error_without_leaking(
    tmp_path,
    monkeypatch,
    capsys,
):
    secret = "ghp_" + "X" * 32
    monkeypatch.setenv("PICO_TEST_SECRET", secret)
    monkeypatch.setattr(
        "pico.workspace.build_trusted_executables",
        Mock(side_effect=RuntimeError("discovery failed " + secret)),
    )

    security = collect_doctor(tmp_path, offline=True)["security"]

    assert security["trusted_executables"] == {
        "status": "degraded",
        "missing": ["git", "rg"],
    }
    assert secret not in json.dumps(security)

    for output_format in ("json", "text"):
        assert main([
            "--cwd",
            str(tmp_path),
            "--format",
            output_format,
            "doctor",
            "--offline",
        ]) == 0
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err


def test_doctor_text_uses_grouped_cli_output(tmp_path, monkeypatch, capsys):
    code = main(["--cwd", str(tmp_path), "doctor", "--offline"])

    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("Pico doctor — CLI health check\n")
    assert "Workspace" in out
    assert "Config" in out
    assert "Credentials" in out
    assert "Storage" in out
    assert "Provider connectivity" in out
    assert "Security" in out
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
            "category": "provider_connectivity",
            "message": "connection timed out",
        }

    monkeypatch.setattr("pico.cli_diagnostics.check_provider_connectivity", fake_connectivity)

    code = main(["--cwd", str(tmp_path), "--format", "json", "doctor"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["provider_connectivity"]["category"] == "provider_connectivity"
    assert payload["data"]["provider_connectivity"]["message"] == "connection timed out"


def test_doctor_rejects_secret_base_url_without_connecting_or_echoing(tmp_path, monkeypatch, capsys):
    called = {}

    def fake_connectivity(config):
        called["connectivity"] = True
        return {"status": "ok"}

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

    assert code == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "provider_base_url_credentials" in output
    assert "user:pass" not in output
    assert "token=" not in output
    assert "secret#frag" not in output
    assert called == {}


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


def test_provider_connectivity_generic_error_sanitizes_url_in_message(monkeypatch):
    def fake_urlopen(url, timeout):
        raise RuntimeError(f"failed to open {url}")

    monkeypatch.setattr("pico.cli_diagnostics.request.urlopen", fake_urlopen)

    result = check_provider_connectivity(
        {
            "provider": {"value": "openai"},
            "base_url": {"value": "https://user:pass@example.com/v1?token=secret#frag"},
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
    (session_dir / "session_1.json").write_text('{"id":"session_1","schema_version":3,"messages":[]}\n', encoding="utf-8")

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
