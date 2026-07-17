import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from unittest.mock import Mock

import pytest

import pico.cli.diagnostics as diagnostics
from pico.cli.app import main
from pico.cli.diagnostics import check_api_connectivity, collect_config, collect_doctor


def _run_git(cwd, *args):
    git = "/usr/bin/git" if Path("/usr/bin/git").is_file() else shutil.which("git")
    return subprocess.run(
        [git or "git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _write_env(root, *, api_base="https://api.anthropic.com/v1", key="secret-value"):
    path = root / ".env"
    path.write_text(
        f"PICO_API_BASE={api_base}\nPICO_API_KEY={key}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_config_show_reports_fixed_contract_and_exact_project_env_path(
    tmp_path, capsys
):
    _write_env(tmp_path)

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "config",
        "show",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload["workspace"] == {"repo_root": str(tmp_path.resolve())}
    assert payload["project_env"] == {
        "path": str(tmp_path.resolve() / ".env"),
        "scope": "repo_root_exact",
        "status": "loaded",
    }
    assert payload["protocol"]["value"] == "anthropic_messages"
    assert payload["provider"]["value"] == "anthropic"
    assert payload["api_variant"]["value"] == "messages"
    assert payload["model"]["value"] == "claude-sonnet-4-6"
    assert payload["auth_mode"]["value"] == "x-api-key"
    assert payload["base_url"] == {
        "value": "https://api.anthropic.com/v1",
        "source": "project_env",
        "name": "PICO_API_BASE",
    }
    assert payload["api_key"] == {
        "present": True,
        "source": "project_env",
        "name": "PICO_API_KEY",
    }
    assert "secret-value" not in json.dumps(payload)


def test_config_show_reports_generic_openai_compatible_base(tmp_path):
    _write_env(tmp_path, api_base="https://gateway.example/v1")

    data = collect_config(tmp_path)

    assert data["base_url"]["value"] == "https://gateway.example/v1"
    assert data["model"]["value"] == "gpt-5.4"


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode assertion")
def test_config_show_preserves_permission_review_after_redactor(tmp_path, capsys):
    env_path = _write_env(tmp_path)
    env_path.chmod(0o644)

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "config",
        "show",
            ]
        )
        == 0
    )

    project_env = json.loads(capsys.readouterr().out)["data"]["project_env"]
    assert project_env["status"] == "review_required"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_config_isolates_main_and_linked_worktree_env(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    main_root = tmp_path / "main"
    linked_root = tmp_path / "linked"
    main_root.mkdir()
    _run_git(main_root, "init", "-q")
    _run_git(main_root, "config", "user.name", "Pico Test")
    _run_git(main_root, "config", "user.email", "pico@example.invalid")
    (main_root / "README.md").write_text("fixture\n", encoding="utf-8")
    _run_git(main_root, "add", "README.md")
    _run_git(main_root, "commit", "-qm", "fixture")
    _run_git(main_root, "worktree", "add", "-q", "-b", "linked", str(linked_root))
    _write_env(main_root, api_base="https://main.example/v1", key="main-key")
    _write_env(linked_root, api_base="https://linked.example/v1", key="linked-key")
    child = linked_root / "src"
    child.mkdir()

    main_data = collect_config(main_root)
    linked_data = collect_config(child)

    assert main_data["base_url"]["value"] == "https://main.example/v1"
    assert linked_data["base_url"]["value"] == "https://linked.example/v1"
    assert main_data["project_env"]["path"] != linked_data["project_env"]["path"]


@pytest.mark.parametrize("unsafe_kind", ("symlink", "hardlink", "directory"))
def test_config_show_fails_closed_for_unsafe_project_env(tmp_path, capsys, unsafe_kind):
    canary = "project-env-outside-canary"
    outside = tmp_path.parent / f"{tmp_path.name}-outside-env"
    env_path = tmp_path / ".env"
    if unsafe_kind == "directory":
        env_path.mkdir()
        (env_path / "canary").write_text(canary, encoding="utf-8")
    else:
        outside.write_text(f"PICO_API_KEY={canary}\n", encoding="utf-8")
        if unsafe_kind == "symlink":
            env_path.symlink_to(outside)
        else:
            os.link(outside, env_path)

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "config",
        "show",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    metadata = json.loads(captured.out)["data"]["project_env"]
    assert metadata["status"] == "review_required"
    assert canary not in captured.out + captured.err
    assert str(outside) not in captured.out + captured.err


def test_config_show_skips_malformed_env_line_without_leaking_key(tmp_path, capsys):
    path = tmp_path / ".env"
    path.write_text(
        "PICO_API_BASE=https://gateway.example/v1\n"
        "not a valid env line\n"
        "PICO_API_KEY=secret-value\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "config",
        "show",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)["data"]
    assert payload["api_key"]["present"] is True
    assert payload["project_env"]["status"] == "review_required"
    assert "warning: skipped invalid .env line 2" in captured.err
    assert "secret-value" not in captured.out


def test_config_show_text_is_grouped_and_never_prints_key(tmp_path, capsys):
    _write_env(tmp_path)

    assert main(["--cwd", str(tmp_path), "config", "show"]) == 0

    output = capsys.readouterr().out
    assert output.startswith("Pico config — Effective configuration\n")
    assert "Model" in output
    assert "claude-sonnet-4-6" in output
    assert "https://api.anthropic.com/v1" in output
    assert "Credentials" in output
    assert "secret-value" not in output


def test_status_reports_model_and_storage_without_building_agent(
    tmp_path, monkeypatch, capsys
):
    _write_env(tmp_path)
    (tmp_path / ".pico" / "sessions").mkdir(parents=True)
    (tmp_path / ".pico" / "runs" / "run_1").mkdir(parents=True)
    monkeypatch.setattr(
        "pico.cli.app.build_agent",
        Mock(side_effect=AssertionError("status must not build an agent")),
    )

    assert main(["--cwd", str(tmp_path), "--format", "json", "status"]) == 0

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload["model"]["provider"]["value"] == "anthropic"
    assert payload["model"]["model"]["value"] == "claude-sonnet-4-6"
    assert payload["storage"]["sessions"] is True
    assert payload["latest"]["run_id"] == "run_1"


def test_doctor_defaults_to_zero_api_requests(tmp_path, monkeypatch, capsys):
    checker = Mock(side_effect=AssertionError("doctor attempted an API request"))
    monkeypatch.setattr(diagnostics, "check_api_connectivity", checker)

    assert main(["--cwd", str(tmp_path), "--format", "json", "doctor"]) == 0

    data = json.loads(capsys.readouterr().out)["data"]
    assert data["api_check"] == {
        "status": "skipped",
        "category": "api_protocol",
        "message": "explicit --check-api not requested",
    }
    checker.assert_not_called()


def test_doctor_marks_ollama_api_key_not_required(tmp_path):
    (tmp_path / ".env").write_text(
        "PICO_API_BASE=http://127.0.0.1:11434\n"
        "PICO_MODEL=qwen3:8b\n"
        "PICO_API_KEY=\n",
        encoding="utf-8",
    )

    data = collect_doctor(tmp_path)

    assert data["config"]["provider"]["value"] == "ollama"
    assert data["config"]["protocol"]["value"] == "ollama_chat"
    assert data["credentials"]["status"] == "not_required"


def test_doctor_check_api_is_the_only_explicit_network_switch(
    tmp_path, monkeypatch, capsys
):
    _write_env(tmp_path)
    checker = Mock(
        return_value={
            "status": "ok",
            "category": "ok",
            "reason_code": "api_verified",
            "stage": "complete",
            "model_calls": 3,
        }
    )
    monkeypatch.setattr(diagnostics, "check_api_connectivity", checker)
    monkeypatch.setattr(
        "pico.cli.app.build_agent",
        Mock(side_effect=AssertionError("doctor must not build an agent")),
    )

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "doctor",
        "--check-api",
            ]
        )
        == 0
    )

    data = json.loads(capsys.readouterr().out)["data"]
    assert data["api_check"]["model_calls"] == 3
    checker.assert_called_once()


def test_doctor_check_api_failure_returns_error_envelope(tmp_path, monkeypatch, capsys):
    _write_env(tmp_path)
    monkeypatch.setattr(
        diagnostics,
        "check_api_connectivity",
        Mock(
            return_value={
            "status": "failed",
            "category": "authentication_failed",
            "reason_code": "authentication_failed",
            "stage": "text",
            "model_calls": 1,
            "http_status": 401,
            }
        ),
    )

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "doctor",
        "--check-api",
            ]
        )
        == 1
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "api_check_failed"
    assert payload["error"]["details"]["reason_code"] == "authentication_failed"


@pytest.mark.parametrize("argument", ("--check-provider", "--offline", "extra"))
def test_doctor_rejects_removed_or_unknown_arguments(tmp_path, argument, capsys):
    assert main(["--cwd", str(tmp_path), "doctor", argument]) == 2
    assert capsys.readouterr().err.strip() == "usage: pico doctor [--check-api]"


def test_doctor_rejects_credentialed_url_without_connecting_or_echoing(
    tmp_path, monkeypatch, capsys
):
    secret = "url-secret-canary"
    _write_env(tmp_path, api_base=f"https://user:{secret}@example.com/v1")
    checker = Mock(side_effect=AssertionError("unsafe URL attempted connection"))
    monkeypatch.setattr(diagnostics, "check_api_connectivity", checker)

    assert (
        main(
            [
        "--cwd",
        str(tmp_path),
        "doctor",
        "--check-api",
            ]
        )
        == 3
    )

    captured = capsys.readouterr()
    assert captured.err.strip() == "api_base_credentials"
    assert secret not in captured.out + captured.err
    checker.assert_not_called()


def _api_config(*, key="test-key"):
    return {
        "protocol": {"value": "anthropic_messages"},
        "model": {"value": "claude-sonnet-4-6"},
        "base_url": {"value": "https://gateway.example/v1"},
        "auth_mode": {"value": "x-api-key"},
        "api_key": {"value": key},
        "capabilities": {
            "prompt_cache": True,
            "strict_tools": True,
            "parallel_tool_control": True,
        },
    }


def test_api_check_without_key_performs_zero_requests(monkeypatch):
    constructor = Mock(side_effect=AssertionError("client must not be built"))
    monkeypatch.setattr(
        "pico.providers.factory.build_transport_client",
        constructor,
    )

    result = check_api_connectivity(_api_config(key=""))

    assert result["reason_code"] == "api_key_not_configured"
    constructor.assert_not_called()


def test_api_check_builds_resolved_anthropic_client_and_reports_probe(monkeypatch):
    client = object()
    constructor = Mock(return_value=client)
    monkeypatch.setattr(
        "pico.providers.factory.build_transport_client",
        constructor,
    )
    monkeypatch.setattr(
        "pico.providers.probe.probe_model_client",
        Mock(
            return_value={
            "status": "ok",
            "stage": "complete",
            "category": "ok",
            "model_calls": 3,
            "binding": {},
            }
        ),
    )

    result = check_api_connectivity(_api_config())

    assert result["reason_code"] == "api_verified"
    assert result["model_calls"] == 3
    assert constructor.call_args.args == ("anthropic_messages",)
    assert constructor.call_args.kwargs == {
        "model": "claude-sonnet-4-6",
        "base_url": "https://gateway.example/v1",
        "api_key": "test-key",
        "timeout": 2,
        "auth_mode": "x-api-key",
        "capabilities": {
            "prompt_cache": True,
            "strict_tools": True,
            "parallel_tool_control": True,
        },
    }


def test_api_check_preserves_safe_http_failure_classification(monkeypatch):
    monkeypatch.setattr(
        "pico.providers.factory.build_transport_client",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(
        "pico.providers.probe.probe_model_client",
        Mock(
            return_value={
            "status": "failed",
            "stage": "text",
            "category": "authentication_failed",
            "model_calls": 1,
            "binding": {},
            "error_code": "http_4xx",
            "http_status": 401,
            }
        ),
    )

    result = check_api_connectivity(_api_config())

    assert result["reason_code"] == "authentication_failed"
    assert result["error_code"] == "http_4xx"
    assert result["http_status"] == 401


def test_collect_doctor_folds_unavailable_workspace_to_safe_shape(tmp_path):
    cwd = tmp_path / "doctor-loop-canary"
    cwd.symlink_to(cwd.name)

    data = collect_doctor(cwd)

    assert data["workspace"] == {"status": "review_required", "repo_root": ""}
    assert data["api_check"]["status"] == "skipped"
    rendered = json.dumps(data)
    assert "doctor-loop-canary" not in rendered
    assert "RuntimeError" not in rendered


def test_doctor_security_requires_review_for_pending_tool_change(tmp_path):
    from pico.state.checkpoint_store import CheckpointStore
    from pico.tools.change_recorder import ToolChangeRecorder

    store = CheckpointStore(tmp_path)
    ToolChangeRecorder(store, owner_id="doctor-test").start(
        "", "turn", "write_file", "workspace_write", {"path": "x.txt"}
    )

    security = collect_doctor(tmp_path)["security"]

    assert security["status"] == "review_required"
    assert security["recovery_review"]["pending_count"] == 1


def test_doctor_runtime_authorization_projection_drops_unknown_fields(
    tmp_path, monkeypatch, capsys
):
    secret = "ghp_" + "Z" * 32
    authorization = {
        "status": "enabled",
        "kind": "local",
        "reason_code": "local_authorization_verified",
        "token": secret,
    }
    monkeypatch.setattr(
        diagnostics,
        "_collect_docker_sandbox_diagnostic",
        lambda **_kwargs: {
            "status": "ready",
            "reason_code": "ready",
            "readiness": {
                "status": "ready",
                "runtime_authorization": dict(authorization),
            },
            "runtime_authorization": dict(authorization),
            "checks": {
                "runtime_authorization": {
                    "status": "pass",
                    "reason_code": "local_authorization_verified",
                    "remediation": "",
                    "token": secret,
                }
            },
        },
    )

    assert main(["--cwd", str(tmp_path), "--format", "json", "doctor"]) == 0

    output = capsys.readouterr().out
    sandbox = json.loads(output)["data"]["sandbox"]
    assert set(sandbox["runtime_authorization"]) == {
        "status",
        "kind",
        "reason_code",
    }
    assert secret not in output


@pytest.mark.parametrize(
    ("has_claude", "has_agents", "expected"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_doctor_project_document_hint(tmp_path, has_claude, has_agents, expected):
    if has_claude:
        (tmp_path / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
    if has_agents:
        (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")

    hints = collect_doctor(tmp_path)["project_docs"]["hints"]

    assert bool(hints) is expected


def test_doctor_text_output_is_grouped(tmp_path, capsys):
    assert main(["--cwd", str(tmp_path), "doctor"]) == 0

    output = capsys.readouterr().out
    assert output.startswith("Pico doctor — CLI health check\n")
    assert "Config" in output
    assert "Credentials" in output
    assert "API check" in output
    assert "Security" in output
    assert "skipped" in output
