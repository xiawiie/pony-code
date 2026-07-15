from pathlib import Path

import pytest

from pico.cli_diagnostics import collect_doctor
from pico.docker_sandbox import DockerSandboxError


EMPTY_CAPACITY = {
    "active_count": 0,
    "pending_count": 0,
    "cleanup_pending_count": 0,
    "staging_bytes": 0,
    "oldest_age_seconds": 0,
    "orphan_verified_count": 0,
    "orphan_unknown_count": 0,
    "reconciliation_required_count": 0,
}


def _ready_status(*, capacity=None):
    return {
        "record_type": "docker_sandbox_status",
        "format_version": 1,
        "status": "ready",
        "reason_code": "ready",
        "platform_profile": "desktop_vm",
        "client_version": "29.5.2",
        "server_version": "29.5.2",
        "api_version": "1.54",
        "server_os": "linux",
        "server_arch": "arm64",
        "endpoint_kind": "local_unix",
        "security": {
            "rootless": False,
            "seccomp": "builtin",
            "cgroup_limits": True,
            "eci": "unknown",
        },
        "image": {
            "present": True,
            "digest_match": True,
            "platform_match": True,
        },
        "network_performed": False,
        "mutation_performed": False,
        "capacity": dict(EMPTY_CAPACITY if capacity is None else capacity),
        "runtime_authorization": {
            "status": "enabled",
            "kind": "local",
            "reason_code": "local_authorization_verified",
        },
        "product_enablement": {
            "status": "blocked",
            "reason_code": "sandbox_product_not_enabled",
        },
    }


def test_doctor_reports_docker_unavailable_without_creating_state(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(
        "pico.cli_docker_sandbox.discover_local_docker",
        lambda: (_ for _ in ()).throw(
            DockerSandboxError("docker_cli_unavailable")
        ),
    )

    sandbox = collect_doctor(tmp_path, offline=True)["sandbox"]

    assert sandbox["status"] == "not_ready"
    assert sandbox["reason_code"] == "docker_cli_unavailable"
    assert sandbox["implementation"] == "docker_container"
    assert sandbox["offline"] is True
    assert sandbox["readiness"]["network_performed"] is False
    assert sandbox["readiness"]["mutation_performed"] is False
    assert sandbox["checks"]["readiness"]["status"] == "fail"
    assert sandbox["checks"]["runtime_authorization"] == {
        "status": "pass",
        "reason_code": "local_authorization_verified",
        "remediation": "",
    }
    assert sandbox["checks"]["product_enablement"] == {
        "status": "not_applicable",
        "reason_code": "sandbox_product_not_enabled",
        "remediation": "",
    }
    assert not (home / ".pico").exists()


def test_doctor_uses_local_authorization_when_docker_is_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "pico.cli_docker_sandbox.sandbox_status_payload",
        _ready_status,
    )

    sandbox = collect_doctor(tmp_path, offline=True)["sandbox"]

    assert sandbox["status"] == "ready"
    assert sandbox["reason_code"] == "ready"
    assert sandbox["checks"]["readiness"]["status"] == "pass"
    assert sandbox["checks"]["state_integrity"]["status"] == "pass"
    assert sandbox["checks"]["runtime_authorization"]["status"] == "pass"
    assert sandbox["product_enablement"]["status"] == "blocked"


def test_doctor_reports_unknown_sandbox_state_without_disclosing_paths(
    tmp_path,
    monkeypatch,
):
    capacity = {**EMPTY_CAPACITY, "orphan_unknown_count": 2}
    monkeypatch.setattr(
        "pico.cli_docker_sandbox.sandbox_status_payload",
        lambda: _ready_status(capacity=capacity),
    )

    sandbox = collect_doctor(tmp_path, offline=True)["sandbox"]

    assert sandbox["checks"]["state_integrity"] == {
        "status": "review_required",
        "reason_code": "sandbox_state_invalid",
        "remediation": "pico sandbox list",
    }
    assert str(tmp_path) not in str(sandbox)


def test_doctor_maps_unexpected_sandbox_error_to_fixed_reason(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "pico.cli_docker_sandbox.sandbox_status_payload",
        lambda: (_ for _ in ()).throw(RuntimeError("private detail")),
    )

    sandbox = collect_doctor(tmp_path, offline=True)["sandbox"]

    assert sandbox["reason_code"] == "sandbox_diagnostic_failed"
    assert sandbox["readiness"]["reason_code"] == "sandbox_diagnostic_failed"
    assert "private detail" not in str(sandbox)


def test_online_doctor_does_not_use_http_for_sandbox_status(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "pico.cli_docker_sandbox.sandbox_status_payload",
        _ready_status,
    )
    monkeypatch.setattr(
        "pico.cli_diagnostics.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("doctor must not network")
        ),
    )

    data = collect_doctor(tmp_path, offline=False)

    assert data["provider_connectivity"]["status"] == "skipped"
    assert data["sandbox"]["reason_code"] == "ready"


@pytest.mark.parametrize("offline", (False, True))
def test_doctor_sandbox_shape_is_stable(tmp_path, monkeypatch, offline):
    monkeypatch.setattr(
        "pico.cli_docker_sandbox.sandbox_status_payload",
        _ready_status,
    )

    sandbox = collect_doctor(tmp_path, offline=offline)["sandbox"]

    assert set(sandbox) == {
        "status",
        "reason_code",
        "implementation",
        "offline",
        "readiness",
        "runtime_authorization",
        "product_enablement",
        "checks",
    }
    assert tuple(sandbox["checks"]) == (
        "readiness",
        "state_integrity",
        "runtime_authorization",
        "product_enablement",
    )
