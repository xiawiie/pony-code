from pico.sandbox_lifecycle import (
    DOCTOR_CHECK_ORDER,
    build_compatibility_payload,
    build_doctor_payload,
)


def test_compatibility_is_exact_and_unknown_checks_fail_closed():
    payload = build_compatibility_payload(
        required={"identity": "a", "platform": "darwin", "arch": "arm64"},
        actual={"identity": "a", "platform": "darwin", "arch": "x64"},
    )
    assert payload["compatible"] is False
    assert payload["record_type"] == "sandbox_compatibility_matrix"
    report = build_doctor_payload(
        compatibility=payload,
        inventory=[],
        checks={
            "platform": True,
            "private_permissions": True,
            "toolchain_identity": {
                "status": "fail",
                "reason_code": "toolchain_corrupt",
                "remediation": "pico sandbox repair",
            },
            "os_capability": True,
            "policy_build": True,
            "minimal_smoke": True,
            "workspace_migration": True,
        },
    )
    assert report["ok"] is False
    assert tuple(report["checks"]) == DOCTOR_CHECK_ORDER
    assert report["checks"]["os_capability"] == {
        "check_id": "os_capability",
        "status": "unknown",
        "reason_code": "blocked_by_toolchain_identity",
        "remediation": "pico sandbox repair",
    }


def test_compatibility_requires_release_smoke_evidence_and_carries_fixed_matrix_fields():
    required = {
        "identity": "bundle-1",
        "platform": "darwin",
        "arch": "arm64",
        "node_version": "24.18.0",
        "srt_version": "0.0.65",
    }
    actual = {
        **required,
        "pico_version": "0.1.0",
        "python_version": "3.12.4",
        "kernel": "24.5.0",
        "bwrap": "not_applicable",
        "userns": "not_applicable",
        "seccomp": "not_applicable",
    }

    unknown = build_compatibility_payload(required=required, actual=actual)
    assert unknown["status"] == "unknown"
    assert unknown["compatible"] is None
    assert unknown["last_smoke_commit"] == ""
    assert {
        "pico_version", "python_version", "node_version", "srt_version",
        "os", "architecture", "kernel", "bwrap", "userns", "seccomp",
    } <= set(unknown)

    verified = build_compatibility_payload(
        required=required,
        actual=actual,
        verification_status="verified",
        last_smoke_commit="2fdac5d",
    )
    assert verified["status"] == "verified"
    assert verified["compatible"] is True
