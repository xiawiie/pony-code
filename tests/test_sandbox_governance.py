from pico.evaluation.sandbox_governance import (
    REQUIRED_CRITERIA,
    REQUIRED_EVIDENCE,
    build_default_on_evidence,
)


def test_incomplete_default_on_evidence_keeps_explicit_mode():
    result = build_default_on_evidence(
        {"host_fallback_count": 0},
        criteria={"macos_ga": True, "linux_ga": True},
    )
    assert result["decision"] == "continue_explicit_on"
    assert result["missing_evidence"]
    assert result["remote_telemetry"] is False


def test_complete_evidence_only_authorizes_a_separate_spec():
    evidence = {
        "platform_coverage": {"macos": True, "linux": True},
        "first_install_success_rate": 1.0,
        "first_install_duration_ms": 100,
        "warm_overhead_ms": 10,
        "not_ready_rate": 0.0,
        "false_deny_categories": ["none_observed"],
        "explicit_disable_rate": 0.0,
        "git_write_impact": {"documented": True},
        "security_incidents": 0,
        "host_fallback_count": 0,
        "rollback_drills": 1,
        "support_cost": 0,
        "stable_release_cycles": 2,
    }
    result = build_default_on_evidence(
        evidence,
        criteria={name: True for name in REQUIRED_CRITERIA},
    )
    assert result["decision"] == "eligible_for_separate_spec"
    assert "default_on" not in result


def test_unknown_empty_or_partial_criteria_fail_closed():
    evidence = {name: None for name in REQUIRED_EVIDENCE}
    evidence.update(stable_release_cycles=2, host_fallback_count=0)

    result = build_default_on_evidence(evidence, criteria={"macos_ga": True})

    assert result["decision"] == "continue_explicit_on"
    assert result["invalid_evidence"]
    assert set(result["invalid_criteria"]) == {"linux_ga", "support_ready"}


def test_security_incident_or_unverified_platform_blocks_eligibility():
    evidence = {
        "platform_coverage": {"macos": True, "linux": False},
        "first_install_success_rate": 1.0,
        "first_install_duration_ms": 100,
        "warm_overhead_ms": 10,
        "not_ready_rate": 0.0,
        "false_deny_categories": ["none_observed"],
        "explicit_disable_rate": 0.0,
        "git_write_impact": {"documented": True},
        "security_incidents": 1,
        "host_fallback_count": 0,
        "rollback_drills": 1,
        "support_cost": 0,
        "stable_release_cycles": 2,
    }

    result = build_default_on_evidence(
        evidence,
        criteria={name: True for name in REQUIRED_CRITERIA},
    )

    assert result["decision"] == "continue_explicit_on"
