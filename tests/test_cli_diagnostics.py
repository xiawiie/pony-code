import json
import os
import shutil
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
from urllib import error

import pytest

import pico.cli_diagnostics as cli_diagnostics_module
from pico.cli import main
from pico.cli_diagnostics import check_provider_connectivity, collect_config, collect_doctor


def _run_git(cwd, *args):
    return subprocess.run(
        [shutil.which("git") or "git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_config_show_reports_exact_project_env_path(tmp_path, capsys):
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=deepseek\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").chmod(0o600)

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "config",
        "show",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload["workspace"] == {"repo_root": str(tmp_path.resolve())}
    assert payload["project_env"] == {
        "path": str(tmp_path.resolve() / ".env"),
        "scope": "repo_root_exact",
        "status": "loaded",
    }
    assert payload["base_url"] == {
        "value": "https://api.deepseek.com/anthropic",
        "source": "default",
        "name": "DEFAULT_DEEPSEEK_BASE_URL",
    }


def test_doctor_reports_the_same_project_env_contract(tmp_path, capsys):
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=deepseek\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").chmod(0o600)

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "doctor",
        "--offline",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload["project_env"] == {
        "path": str(tmp_path.resolve() / ".env"),
        "scope": "repo_root_exact",
        "status": "loaded",
    }
    assert payload["security"]["project_env"] == {
        "status": "loaded",
        "mode": "0600",
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode assertion")
def test_config_show_preserves_permission_review_after_redactor(
    tmp_path,
    capsys,
):
    env_path = tmp_path / ".env"
    env_path.write_text("PICO_PROVIDER=deepseek\n", encoding="utf-8")
    env_path.chmod(0o644)

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "config",
        "show",
    ]) == 0

    project_env = json.loads(capsys.readouterr().out)["data"]["project_env"]
    assert project_env["status"] == "review_required"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_config_isolates_main_and_linked_worktree_env(tmp_path, monkeypatch):
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
    _run_git(
        main_root,
        "worktree",
        "add",
        "-q",
        "-b",
        "linked",
        str(linked_root),
    )

    (main_root / ".env").write_text(
        "PICO_PROVIDER=openai\n",
        encoding="utf-8",
    )
    (main_root / ".env").chmod(0o600)
    (linked_root / ".env").write_text(
        "PICO_PROVIDER=deepseek\n",
        encoding="utf-8",
    )
    (linked_root / ".env").chmod(0o600)
    child = linked_root / "src"
    child.mkdir()
    monkeypatch.delenv("PICO_PROVIDER", raising=False)

    main_data = collect_config(main_root)
    linked_data = collect_config(child)

    assert main_data["provider"]["value"] == "openai"
    assert linked_data["provider"]["value"] == "deepseek"
    assert main_data["project_env"]["path"] == str(main_root / ".env")
    assert linked_data["project_env"]["path"] == str(linked_root / ".env")
    assert main_data["project_env"]["path"] != linked_data["project_env"]["path"]

    (linked_root / ".env").unlink()
    missing = collect_config(child)
    assert missing["project_env"]["status"] == "missing"
    assert missing["provider"]["value"] != "openai"


@pytest.mark.parametrize("unsafe_kind", ("symlink", "hardlink", "directory"))
def test_config_show_marks_unsafe_project_env_for_review_without_canary(
    tmp_path,
    capsys,
    unsafe_kind,
):
    canary = "project-env-outside-canary"
    outside = tmp_path.parent / f"{tmp_path.name}-outside-env"
    env_path = tmp_path / ".env"
    if unsafe_kind == "directory":
        env_path.mkdir()
        (env_path / "canary").write_text(canary, encoding="utf-8")
    else:
        outside.write_text(
            f"PICO_PROVIDER=deepseek\n{canary}\n",
            encoding="utf-8",
        )
        if unsafe_kind == "symlink":
            env_path.symlink_to(outside)
        else:
            os.link(outside, env_path)

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "config",
        "show",
    ]) == 0

    captured = capsys.readouterr()
    metadata = json.loads(captured.out)["data"]["project_env"]
    assert metadata["scope"] == "repo_root_exact"
    assert metadata["status"] == "review_required"
    assert canary not in captured.out + captured.err
    assert str(outside) not in captured.out + captured.err


def _clear_provider_env(monkeypatch):
    for name in (
        "PICO_PROVIDER",
        "PICO_DEEPSEEK_MODEL",
        "DEEPSEEK_MODEL",
        "PICO_DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _symlink_loop(tmp_path):
    path = tmp_path / "doctor-loop-canary"
    path.symlink_to(path.name)
    return path


def test_doctor_json_exposes_safe_security_contract(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=deepseek\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").chmod(0o600)
    monkeypatch.setattr(
        "pico.cli_diagnostics.build_trusted_executables",
        lambda root, env=None, names=(): {
            "git": "/usr/bin/git",
            "rg": "/usr/bin/rg",
        },
    )

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "doctor",
            "--offline",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    security = payload["data"]["security"]

    assert code == 0
    assert security == {
        "status": "ok",
        "project_env": {"status": "loaded", "mode": "0600"},
        "private_storage": {"status": "missing"},
        "trusted_executables": {"status": "ok", "missing": []},
        "recovery_review": {
            "pending_count": 0,
            "applying_count": 0,
            "unreviewed_partial_count": 0,
            "invalid_mutation_count": 0,
        },
    }
    assert "PICO_PROVIDER=deepseek" not in json.dumps(payload)


def test_doctor_security_requires_review_for_pending_tool_change(
    tmp_path, monkeypatch, capsys
):
    from pico.checkpoint_store import CheckpointStore
    from pico.tool_change_recorder import ToolChangeRecorder

    monkeypatch.setattr(
        "pico.cli_diagnostics.build_trusted_executables",
        lambda root, env=None, names=(): {
            "git": "/usr/bin/git",
            "rg": "/usr/bin/rg",
        },
    )
    store = CheckpointStore(tmp_path)
    ToolChangeRecorder(store, owner_id="doctor-test").start(
        "", "turn", "write_file", "workspace_write", {"path": "x.txt"}
    )

    assert (
        main(
            [
                "--cwd",
                str(tmp_path),
                "--format",
                "json",
                "doctor",
                "--offline",
            ]
        )
        == 0
    )
    security = json.loads(capsys.readouterr().out)["data"]["security"]
    assert security["status"] == "review_required"
    assert security["recovery_review"]["pending_count"] == 1


def test_doctor_fails_closed_for_hardlinked_private_record(
    tmp_path, monkeypatch
):
    if os.name != "posix":
        pytest.skip("POSIX hardlink assertion")
    from pico.checkpoint_store import CheckpointStore

    monkeypatch.setattr(
        "pico.cli_diagnostics.build_trusted_executables",
        lambda root, env=None, names=(): {
            "git": "/usr/bin/git",
            "rg": "/usr/bin/rg",
        },
    )
    store = CheckpointStore(tmp_path)
    record = store.records_dir / "hardlinked.json"
    record.write_text("{}", encoding="utf-8")
    record.chmod(0o600)
    os.link(record, store.records_dir / "hardlinked-copy.json")

    security = collect_doctor(tmp_path, offline=True)["security"]

    assert security["status"] == "review_required"
    assert security["private_storage"] == {"status": "review_required"}
    assert security["recovery_review"]["invalid_mutation_count"] >= 1


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
    (tmp_path / ".env").chmod(0o600)

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
    assert payload["data"]["project_env"]["status"] == "review_required"
    assert "warning: skipped invalid .env line 2" in captured.err
    assert "secret-value" not in captured.out

    assert main([
        "--cwd",
        str(tmp_path),
        "--format",
        "json",
        "doctor",
        "--offline",
    ]) == 0
    doctor = json.loads(capsys.readouterr().out)["data"]
    assert doctor["project_env"]["status"] == "review_required"
    assert doctor["security"]["project_env"]["status"] == "review_required"


def test_config_show_text_uses_grouped_cli_output_without_secret_value(tmp_path, monkeypatch, capsys):
    _clear_provider_env(monkeypatch)
    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=deepseek\nPICO_DEEPSEEK_API_KEY=secret-value\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").chmod(0o600)

    code = main(["--cwd", str(tmp_path), "config", "show"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.startswith("Pico config — Effective configuration\n")
    assert "Workspace" in captured.out
    assert str(tmp_path.resolve()) in captured.out
    assert "Project environment" in captured.out
    assert "repo_root_exact" in captured.out
    assert "loaded" in captured.out
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


def test_collect_doctor_folds_unavailable_workspace_to_fixed_safe_shape(
    tmp_path,
    monkeypatch,
):
    _clear_provider_env(monkeypatch)
    cwd = _symlink_loop(tmp_path)
    connectivity = Mock(side_effect=AssertionError("offline doctor attempted network"))
    monkeypatch.setattr(
        cli_diagnostics_module,
        "check_provider_connectivity",
        connectivity,
    )

    data = collect_doctor(cwd, offline=True)

    assert data["workspace"] == {"status": "review_required", "repo_root": ""}
    assert data["project_env"] == {
        "path": "",
        "scope": "repo_root_exact",
        "status": "review_required",
    }
    assert data["security"] == {
        "status": "review_required",
        "project_env": {"status": "review_required", "mode": ""},
        "private_storage": {"status": "review_required"},
        "trusted_executables": {
            "status": "degraded",
            "missing": ["git", "rg"],
        },
        "recovery_review": {
            "pending_count": 0,
            "applying_count": 0,
            "unreviewed_partial_count": 0,
            "invalid_mutation_count": 0,
        },
    }
    rendered = json.dumps(data)
    assert "doctor-loop-canary" not in rendered
    assert "RuntimeError" not in rendered
    connectivity.assert_not_called()


@pytest.mark.parametrize("output_format", ("json", "text"))
def test_doctor_unavailable_workspace_is_safe_in_real_cli_output(
    tmp_path,
    monkeypatch,
    capsys,
    output_format,
):
    _clear_provider_env(monkeypatch)
    cwd = _symlink_loop(tmp_path)
    connectivity = Mock(side_effect=AssertionError("offline doctor attempted network"))
    inspection_redactor = Mock(
        side_effect=AssertionError("unavailable workspace must not inspect fallback cwd")
    )
    monkeypatch.setattr(
        cli_diagnostics_module,
        "check_provider_connectivity",
        connectivity,
    )
    monkeypatch.setattr(
        cli_diagnostics_module,
        "build_inspection_redactor",
        inspection_redactor,
    )

    code = main(
        [
            "--cwd",
            str(cwd),
            "--format",
            output_format,
            "doctor",
            "--offline",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "review_required" in captured.out
    assert "doctor-loop-canary" not in captured.out + captured.err
    assert "RuntimeError" not in captured.out + captured.err
    connectivity.assert_not_called()
    inspection_redactor.assert_not_called()


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
        "recovery_review",
    }
    assert set(security["project_env"]) == {"status", "mode"}
    assert set(security["private_storage"]) == {"status"}
    assert set(security["trusted_executables"]) == {"status", "missing"}
    assert set(security["recovery_review"]) == {
        "pending_count",
        "applying_count",
        "unreviewed_partial_count",
        "invalid_mutation_count",
    }
    assert all(
        name and "/" not in name and "\\" not in name
        for name in security["trusted_executables"]["missing"]
    )
    assert secret not in json.dumps(security)
    assert str(tmp_path) not in json.dumps(security)
    if os.name == "posix":
        assert security["project_env"] == {
            "status": "review_required",
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
        "recovery_review": {
            "pending_count": 0,
            "applying_count": 0,
            "unreviewed_partial_count": 0,
            "invalid_mutation_count": 0,
        },
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
        "pico.cli_diagnostics.build_trusted_executables",
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
    assert str(tmp_path.resolve()) in out
    assert "Project environment" in out
    assert "repo_root_exact" in out
    assert "missing" in out
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


def test_doctor_flags_claude_md_without_agents_md(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# Claude\n")

    result = collect_doctor(str(tmp_path), SimpleNamespace(cwd=str(tmp_path)), offline=True)

    hints = (result.get("project_docs") or {}).get("hints") or []
    text_dump = " ".join(hint.get("message", "") for hint in hints)
    assert "CLAUDE.md" in text_dump
    assert "AGENTS.md" in text_dump


def test_doctor_no_claude_hint_when_agents_md_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Agents\n")
    (tmp_path / "CLAUDE.md").write_text("# Claude\n")

    result = collect_doctor(str(tmp_path), SimpleNamespace(cwd=str(tmp_path)), offline=True)

    hints = (result.get("project_docs") or {}).get("hints") or []
    assert all("CLAUDE.md" not in hint.get("message", "") for hint in hints)


def test_doctor_no_project_doc_hint_when_neither_file_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = collect_doctor(str(tmp_path), SimpleNamespace(cwd=str(tmp_path)), offline=True)

    assert ((result.get("project_docs") or {}).get("hints") or []) == []


def test_doctor_text_output_shows_claude_md_hint(tmp_path, monkeypatch, capsys):
    from pico.cli_diagnostics import handle_doctor

    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# Claude\n")

    rc = handle_doctor(
        ["--offline"],
        str(tmp_path),
        SimpleNamespace(format="text", cwd=str(tmp_path)),
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "CLAUDE.md" in out
    assert "AGENTS.md" in out
