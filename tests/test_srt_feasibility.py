import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/srt_feasibility.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("srt_feasibility", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_srt_feasibility_help_is_available():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert "--real" in result.stdout
    assert "--format" in result.stdout


def test_srt_feasibility_offline_report_is_low_sensitivity(tmp_path):
    module = _load_script()

    report = module.build_report(
        platform_name="darwin",
        architecture="arm64",
        srt_path=None,
        node_path=None,
        real=False,
    )

    harness = report["harness"]
    assert set(harness) == {"commit", "digest", "dirty"}
    assert harness["digest"].startswith("sha256:")
    assert type(harness["dirty"]) is bool
    comparable = dict(report)
    comparable.pop("harness")
    assert comparable == {
        "record_type": "srt_feasibility",
        "format_version": 1,
        "platform": "darwin",
        "architecture": "arm64",
        "mode": "offline",
        "status": "failed",
        "reason_code": "candidate_rejected",
        "candidate": {
            "node_version": "24.18.0",
            "srt_package": "@anthropic-ai/sandbox-runtime",
            "srt_version": "0.0.65",
            "srt_integrity": (
                "sha512-0uW2bMIBLT45tehULlohOnco71xCJzrb4h7pQSUnMYfMJAJ77s"
                "MAI3Q9jP2h973hw5tg6dfEjyayc85rXixuAg=="
            ),
        },
        "versions": {
            "node_candidate": "24.18.0",
            "srt_candidate": "0.0.65",
            "srt_source_revision": (
                "npm-sha512-0uW2bMIBLT45tehULlohOnco71xCJzrb4h7pQSUnMYfMJAJ77s"
                "MAI3Q9jP2h973hw5tg6dfEjyayc85rXixuAg=="
            ),
        },
        "checks": [
            {
                "check_id": "future_sensitive_workspace_write",
                "mandatory": True,
                "status": "fail",
                "reason_code": "srt_0_0_65_linux_deny_write_glob_skipped",
            }
        ],
        "mandatory_passed": 0,
        "mandatory_failed": 1,
        "host_fallback_count": None,
    }
    serialized = json.dumps(report)
    assert str(tmp_path) not in serialized
    assert "stdout" not in serialized
    assert "stderr" not in serialized


def test_srt_feasibility_uses_fixed_check_ids():
    module = _load_script()

    assert module.MANDATORY_CHECK_IDS == (
        "settings_schema",
        "workspace_read",
        "workspace_write",
        "workspace_sibling_read",
        "ordinary_home_read",
        "sensitive_workspace_read",
        "sensitive_workspace_write",
        "future_sensitive_workspace_write",
        "git_metadata_write",
        "external_write",
        "external_tcp",
        "external_udp",
        "dns_resolution",
        "localhost_ipv4",
        "localhost_ipv6",
        "listener_ipv4",
        "listener_ipv6",
        "unix_socket_connect",
        "unix_socket_create",
        "child_process_inheritance",
        "grandchild_process_inheritance",
        "linux_proc_host_env",
        "linux_dev_shm",
        "timeout_cleanup",
        "detached_setsid_cleanup",
        "argv_fidelity",
        "target_nonzero",
        "wrapper_bootstrap",
        "wrapper_cleanup",
        "platform_provenance",
        "workspace_first_read_frontier",
        "credential_read",
        "user_memory_read",
        "symlink_escape_read",
        "ordinary_home_write",
        "toolchain_write",
        "external_git_metadata_write",
        "user_notes_write",
        "future_nested_protected_write",
        "macos_apple_events",
        "macos_keychain",
        "macos_clipboard",
        "host_fallback_trap",
        "host_fd_inheritance",
        "target_not_started",
        "sigint_cleanup",
        "sigterm_cleanup",
        "detached_double_fork_cleanup",
        "helper_residue",
    )

    assert module._pending_mandatory_checks() == [
        {
            "check_id": check_id,
            "mandatory": True,
            "status": "not_ready",
            "reason_code": "probe_not_implemented",
        }
        for check_id in module.PENDING_MANDATORY_CHECK_IDS
    ]


def test_host_fallback_count_requires_real_execution_records(tmp_path, monkeypatch):
    module = _load_script()
    monkeypatch.setattr(module, "_run", lambda *_args, **_kwargs: (0, False))

    assert module._host_fallback_count([]) is None
    with module._capture_execution_records() as records:
        module._sandbox_command(
            (Path("/trusted/node"), Path("/trusted/srt.js")),
            tmp_path / "settings.json",
            ["/usr/bin/true"],
            cwd=tmp_path,
        )

    assert records == ["srt"]
    assert module._host_fallback_count(records) == 0
    assert module._host_fallback_count(["srt", "host", "other"]) == 2


def test_host_fallback_failure_overrides_pending_probe_status():
    module = _load_script()
    checks = [
        module._not_ready(check_id, "probe_not_implemented")
        for check_id in module.MANDATORY_CHECK_IDS
    ]
    report = module._base_report(
        platform_name="darwin",
        architecture="arm64",
        real=True,
    )
    report["checks"] = checks
    report["mandatory_failed"] = len(checks)
    report["host_fallback_count"] = 1

    module._finalize_probe_status(report, checks)

    assert report["status"] == "failed"
    assert report["reason_code"] == "host_fallback_detected"


def test_pending_probe_reason_is_not_mislabeled_as_positive_control_failure():
    module = _load_script()
    checks = [
        module._not_ready(check_id, "probe_not_implemented")
        for check_id in module.MANDATORY_CHECK_IDS
    ]
    report = module._base_report(
        platform_name="linux",
        architecture="x86_64",
        real=True,
    )
    report["checks"] = checks
    report["mandatory_failed"] = len(checks)
    report["host_fallback_count"] = 0

    module._finalize_probe_status(report, checks)

    assert report["status"] == "not_ready"
    assert report["reason_code"] == "mandatory_probe_not_implemented"


def test_repeat_aggregate_preserves_observed_host_fallback_failure(
    monkeypatch,
    capsys,
):
    module = _load_script()
    reports = [
        {
            **module._base_report(
                platform_name="darwin",
                architecture="arm64",
                real=True,
            ),
            "status": "not_ready",
            "reason_code": "mandatory_probe_not_implemented",
        },
        {
            **module._base_report(
                platform_name="darwin",
                architecture="arm64",
                real=True,
            ),
            "status": "failed",
            "reason_code": "host_fallback_detected",
            "host_fallback_count": 1,
        },
    ]
    monkeypatch.setattr(module, "run_real_probe", lambda **_kwargs: reports.pop(0))

    exit_code = module.main(["--real", "--repeat", "2", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "host_fallback_detected"
    assert payload["host_fallback_count"] == 1


def test_srt_feasibility_rejects_unknown_settings_keys(tmp_path):
    module = _load_script()
    valid = json.loads(module._settings(tmp_path).read_text())

    assert module._validate_settings_payload(valid) == valid
    assert set(valid) == {
        "network",
        "filesystem",
        "enableWeakerNestedSandbox",
        "enableWeakerNetworkIsolation",
        "allowAppleEvents",
    }
    assert valid["network"] == {
        "allowedDomains": [],
        "deniedDomains": ["*"],
        "strictAllowlist": True,
        "allowLocalBinding": False,
        "allowUnixSockets": [],
        "allowAllUnixSockets": False,
    }
    settings = module._settings(tmp_path, unknown=True)

    with pytest.raises(ValueError, match="invalid settings schema"):
        module._validate_settings_payload(json.loads(settings.read_text()))

    invalid_payloads = []
    for path, value in (
        (("network", "strictAllowlist"), 1),
        (("filesystem", "allowWrite"), ["relative"]),
        (("allowAppleEvents",), True),
    ):
        invalid = json.loads(json.dumps(valid))
        target = invalid
        for name in path[:-1]:
            target = target[name]
        target[path[-1]] = value
        invalid_payloads.append(invalid)
    invalid = json.loads(json.dumps(valid))
    invalid["network"]["unknown"] = False
    invalid_payloads.append(invalid)

    for invalid in invalid_payloads:
        with pytest.raises(ValueError, match="invalid settings schema"):
            module._validate_settings_payload(invalid)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"network":{},"network":{}}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid settings schema"):
        module._load_settings_payload(duplicate)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (module.MAX_SETTINGS_BYTES + 1))
    with pytest.raises(ValueError, match="invalid settings schema"):
        module._load_settings_payload(oversized)

    too_many_paths = json.loads(json.dumps(valid))
    too_many_paths["filesystem"]["denyRead"] = [
        f"/deny/{index}" for index in range(module.MAX_SETTINGS_PATHS + 1)
    ]
    with pytest.raises(ValueError, match="invalid settings schema"):
        module._validate_settings_payload(too_many_paths)


def test_srt_feasibility_schema_probe_only_proves_pico_exact_validator(
    tmp_path, monkeypatch
):
    module = _load_script()
    monkeypatch.setattr(
        module,
        "_sandbox_command",
        lambda *args, **kwargs: pytest.fail("schema probe must not invoke SRT"),
    )
    (tmp_path / "workspace").mkdir()
    check = module._probe_settings_schema(tmp_path)
    assert check["status"] == "pass"
    assert check["reason_code"] == "pico_exact_schema_verified"


def test_source_precheck_can_reject_but_never_approve_candidate(tmp_path, monkeypatch):
    module = _load_script()

    rejected = module.build_report(
        platform_name="linux",
        architecture="x86_64",
        srt_path=None,
        node_path=None,
        real=True,
    )
    assert rejected["status"] == "failed"
    assert rejected["reason_code"] == "candidate_rejected"
    assert rejected["checks"][0]["reason_code"] == (
        "srt_0_0_65_linux_deny_write_glob_skipped"
    )

    monkeypatch.setattr(module, "SRT_CANDIDATE_VERSION", "0.0.66")
    unresolved = module.build_report(
        platform_name="linux",
        architecture="x86_64",
        srt_path=None,
        node_path=None,
        real=True,
    )
    assert unresolved["status"] == "not_ready"
    assert unresolved["reason_code"] == "srt_unavailable"
    assert unresolved["checks"] == []


def test_network_probe_requires_host_positive_control(tmp_path, monkeypatch):
    module = _load_script()
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: (1, False))
    monkeypatch.setattr(
        module,
        "_sandbox_command",
        lambda *args, **kwargs: pytest.fail("sandbox probe must not run"),
    )

    check = module._node_denial_probe(
        "/trusted/srt",
        "/private/settings.json",
        "/trusted/node",
        tmp_path,
        "external_tcp",
        "process.exit(0)",
    )

    assert check == {
        "check_id": "external_tcp",
        "mandatory": True,
        "status": "not_ready",
        "reason_code": "host_positive_control_failed",
    }


def test_network_probe_passes_only_after_positive_control_and_sandbox_denial(
    tmp_path, monkeypatch
):
    module = _load_script()
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: (0, False))
    monkeypatch.setattr(
        module, "_sandbox_command", lambda *args, **kwargs: (0, False)
    )

    check = module._node_denial_probe(
        "/trusted/srt",
        "/private/settings.json",
        "/trusted/node",
        tmp_path,
        "unix_socket_create",
        "process.exit(0)",
    )

    assert check["status"] == "pass"
    assert check["reason_code"] == "blocked_after_host_positive_control"


def test_srt_feasibility_inserts_option_separator():
    module = _load_script()
    captured = {}

    def fake_run(argv, *, cwd, timeout):
        captured["argv"] = argv
        return 0, False

    original = module._run
    module._run = fake_run
    try:
        module._sandbox_command(
            Path("/trusted/srt"),
            Path("/private/settings.json"),
            ["/usr/bin/python3", "-c", "pass"],
            cwd=Path("/workspace"),
        )
    finally:
        module._run = original

    assert captured["argv"] == [
        "/trusted/srt",
        "--settings",
        "/private/settings.json",
        "--",
        "/usr/bin/python3",
        "-c",
        "pass",
    ]


def test_verified_launcher_uses_managed_node_for_js_entry(tmp_path, monkeypatch):
    module = _load_script()
    node = tmp_path / "node"
    node.write_text("node", encoding="utf-8")
    node.chmod(0o700)
    package = tmp_path / "node_modules" / "@anthropic-ai" / "sandbox-runtime"
    entry = package / "dist" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("entry", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps({"name": "@anthropic-ai/sandbox-runtime", "version": "0.0.65"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="v24.18.0\n", stderr=""
        ),
    )

    launcher, reason, node_version, srt_version = module._verified_launcher(
        node, entry
    )

    assert launcher == (node, entry)
    assert reason == "versions_verified"
    assert (node_version, srt_version) == ("24.18.0", "0.0.65")


def test_verified_launcher_rejects_node_version_mismatch(tmp_path, monkeypatch):
    module = _load_script()
    node = tmp_path / "node"
    srt = tmp_path / "srt"
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="v23.0.0\n", stderr=""
        ),
    )
    launcher, reason, *_ = module._verified_launcher(node, srt)
    assert launcher is None
    assert reason == "node_version_mismatch"


def test_srt_feasibility_source_rejects_candidate_without_installed_srt():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--real", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": ""},
    )

    assert result.returncode == 3
    report = json.loads(result.stdout)
    assert report["status"] == "failed"
    assert report["reason_code"] == "candidate_rejected"
    assert report["checks"][0]["check_id"] == "future_sensitive_workspace_write"
    assert report["host_fallback_count"] is None


def test_managed_mode_uses_only_verified_toolchain_paths(monkeypatch):
    module = _load_script()
    node = Path("/trusted/node")
    srt = Path("/trusted/srt.js")
    monkeypatch.setattr(module, "_managed_paths", lambda: (srt, node))
    observed = {}

    def real_probe(**kwargs):
        observed.update(kwargs)
        return {
            "status": "not_ready",
            "reason_code": "test",
            "checks": [],
            "mandatory_passed": 0,
            "mandatory_failed": 0,
            "host_fallback_count": 0,
        }

    monkeypatch.setattr(module, "run_real_probe", real_probe)

    assert module.main(["--real", "--managed", "--format", "json"]) == 2
    assert observed["node_path"] == node
    assert observed["srt_path"] == srt
