"""Pure Release C decision evidence; never changes Sandbox defaults."""

from __future__ import annotations


REQUIRED_EVIDENCE = (
    "platform_coverage",
    "first_install_success_rate",
    "first_install_duration_ms",
    "warm_overhead_ms",
    "not_ready_rate",
    "false_deny_categories",
    "explicit_disable_rate",
    "git_write_impact",
    "security_incidents",
    "host_fallback_count",
    "rollback_drills",
    "support_cost",
    "stable_release_cycles",
)
REQUIRED_CRITERIA = ("macos_ga", "linux_ga", "support_ready")


def _is_number(value):
    return type(value) in {int, float}


def _valid_evidence(name, value):
    if name in {"first_install_success_rate", "not_ready_rate", "explicit_disable_rate"}:
        return _is_number(value) and 0 <= value <= 1
    if name in {"first_install_duration_ms", "warm_overhead_ms", "support_cost"}:
        return _is_number(value) and value >= 0
    if name in {"security_incidents", "host_fallback_count", "rollback_drills", "stable_release_cycles"}:
        return type(value) is int and value >= 0
    if name == "platform_coverage":
        return (
            isinstance(value, dict)
            and set(value) == {"macos", "linux"}
            and all(type(item) is bool for item in value.values())
        )
    if name == "false_deny_categories":
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and item for item in value)
        )
    if name == "git_write_impact":
        return isinstance(value, dict) and bool(value)
    return False


def build_default_on_evidence(evidence, *, criteria):
    """Return a conservative decision package from local/release evidence."""
    evidence = dict(evidence or {})
    criteria = dict(criteria or {})
    missing = [name for name in REQUIRED_EVIDENCE if name not in evidence]
    invalid_evidence = sorted(
        name
        for name in REQUIRED_EVIDENCE
        if name in evidence and not _valid_evidence(name, evidence[name])
    )
    invalid_criteria = sorted(
        set(REQUIRED_CRITERIA).symmetric_difference(criteria)
        | {name for name, value in criteria.items() if type(value) is not bool}
    )
    coverage = evidence.get("platform_coverage")
    eligible = (
        not missing
        and not invalid_evidence
        and not invalid_criteria
        and all(criteria.values())
        and evidence["stable_release_cycles"] >= 2
        and evidence["host_fallback_count"] == 0
        and evidence["security_incidents"] == 0
        and evidence["rollback_drills"] >= 1
        and all(coverage.values())
    )
    return {
        "record_type": "sandbox_default_on_evidence",
        "format_version": 1,
        "decision": (
            "eligible_for_separate_spec" if eligible else "continue_explicit_on"
        ),
        "missing_evidence": missing,
        "invalid_evidence": invalid_evidence,
        "invalid_criteria": invalid_criteria,
        "criteria": criteria,
        "evidence": evidence,
        "remote_telemetry": False,
    }
