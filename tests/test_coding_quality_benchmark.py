import copy
import json
from pathlib import Path
import shutil

import pytest

from benchmarks.coding_quality import run_benchmark
from benchmarks.coding_quality.run_benchmark import (
    compare_artifacts,
    load_evaluation_brief,
    load_tasks,
    qualify_benchmark,
    run_condition,
)
from benchmarks.support.fake_provider import FakeModelClient


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "benchmarks" / "coding_quality" / "tasks.json"


def _head():
    return run_benchmark._git(["rev-parse", "HEAD"], cwd=ROOT)


def _brief(baseline_sha=None, candidate_sha=None, *, trials=1):
    return {
        "record_type": "evaluation_brief",
        "format_version": 1,
        "decision": "decide whether to accept the candidate",
        "question": "does the candidate improve safe completion?",
        "hypothesis": "the candidate fixes the target failure mode",
        "baseline": {"label": "baseline", "commit_sha": baseline_sha or _head()},
        "candidate": {"label": "candidate", "commit_sha": candidate_sha or _head()},
        "target_slices": ["failing-test-diagnosis"],
        "primary_metric": "safe_correct_completion",
        "expected_effect": {"min_task_wins": 1, "max_task_losses": 0},
        "guardrails": {
            "forbid_stable_pass_to_fail": True,
            "max_hard_gate_failures": 0,
        },
        "trials_per_task": trials,
        "decision_rule": "accept only when the expected effect and guardrails pass",
        "inconclusive_conditions": [
            "dirty_worktree",
            "frozen_condition_or_provenance_mismatch",
            "invalid_trial",
            "non_live_provider",
            "provider_transport_failure",
        ],
    }


def _write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _reference_outputs(task):
    outputs = []
    for relative in task["reference_snapshot"]:
        outputs.append(
            {
                "name": "write_file",
                "args": {
                    "path": relative,
                    "content": (task["reference_path"] / relative).read_text(encoding="utf-8"),
                },
            }
        )
    outputs.extend(
        [
            {
                "name": "run_shell",
                "args": {
                    "command": "python3 -m unittest discover -s . -q",
                    "timeout": 30,
                },
            },
            "Implemented and verified.",
        ]
    )
    return outputs


def test_pilot_corpus_is_strict_and_covers_four_decision_slices():
    benchmark = load_tasks(TASKS)

    assert len(benchmark["tasks"]) == 8
    assert {task["slice"] for task in benchmark["tasks"]} == {
        "failing-test-diagnosis",
        "issue-driven-navigation",
        "multi-file-contract",
        "io-config-cli-hardening",
    }
    assert all("run_shell" in task["allowed_tools"] for task in benchmark["tasks"])

    payload = json.loads(TASKS.read_text(encoding="utf-8"))
    payload["tasks"][0]["unexpected"] = True
    with pytest.raises(ValueError, match="invalid task fields"):
        run_benchmark.validate_tasks(payload)


def test_duplicate_json_keys_and_unsafe_fixture_entries_fail_closed(tmp_path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"record_type":"evaluation_brief","record_type":"x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_evaluation_brief(duplicate)

    linked_record = tmp_path / "linked.json"
    linked_record.symlink_to(duplicate)
    with pytest.raises(ValueError, match="unsafe JSON record"):
        load_evaluation_brief(linked_record)

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "safe.py").write_text("value = 1\n", encoding="utf-8")
    (fixture / "escape.py").symlink_to(fixture / "safe.py")
    with pytest.raises(ValueError, match="unsafe entry"):
        run_benchmark._tree_snapshot(fixture)

    external_tasks = _write_json(
        tmp_path / "tasks.json", json.loads(TASKS.read_text(encoding="utf-8"))
    )
    with pytest.raises(ValueError, match="must be inside repository"):
        load_tasks(external_tasks)


def test_evaluation_brief_is_machine_checked_before_results_exist(tmp_path):
    brief = _brief()
    path = _write_json(tmp_path / "brief.json", brief)
    assert load_evaluation_brief(path) == brief

    invalid = copy.deepcopy(brief)
    invalid["expected_effect"]["expression"] = "wins > losses"
    path = _write_json(tmp_path / "invalid.json", invalid)
    with pytest.raises(ValueError, match="invalid expected effect fields"):
        load_evaluation_brief(path)

    invalid = copy.deepcopy(brief)
    invalid["inconclusive_conditions"] = ["whatever the operator decides later"]
    path = _write_json(tmp_path / "invalid-inconclusive.json", invalid)
    with pytest.raises(ValueError, match="invalid inconclusive_conditions"):
        load_evaluation_brief(path)


def test_offline_qualification_proves_broken_reference_and_integrity_contract():
    artifact = qualify_benchmark(TASKS)

    assert artifact["summary"] == {"qualified": 8, "failed": 0}
    assert artifact["claim_scope"] == "offline benchmark and grader qualification only"
    assert all(row["broken_target_failures"] == 3 for row in artifact["tasks"])
    assert all(row["reference_passes"] == 5 for row in artifact["tasks"])
    assert all(row["tamper_rejected"] for row in artifact["tasks"])
    for row in artifact["tasks"]:
        expected = "fail" if row["slice"] == "failing-test-diagnosis" else "pass"
        assert row["broken_public_expected"] == expected
        assert row["broken_public_observed"] == expected


def test_fake_condition_only_proves_runner_plumbing_and_scc_contract(tmp_path):
    brief_path = _write_json(tmp_path / "brief.json", _brief())
    clients = []

    def factory(*, task, workspace, trial):
        assert trial == 1
        workspace_root = Path(workspace.repo_root)
        assert workspace_root.name.startswith(task["id"])
        assert not (workspace_root / "benchmarks" / "coding_quality" / "graders").exists()
        client = FakeModelClient(_reference_outputs(task))
        clients.append(client)
        return client

    artifact = run_condition(
        brief_path=brief_path,
        condition_name="candidate",
        tasks_path=TASKS,
        output_path=tmp_path / "condition.json",
        model_client_factory=factory,
        allow_dirty=True,
    )

    assert len(clients) == 2
    assert artifact["provider"]["kind"] == "scripted"
    assert artifact["claim_scope"] == "non-confirmatory plumbing or dirty-worktree pilot"
    assert artifact["summary"]["safe_correct_completions"] == 2
    assert artifact["summary"]["invalid_trials"] == 0
    assert all(row["scc_count"] == 1 for row in artifact["tasks"])
    assert all(row["trials"][0]["self_verification"] for row in artifact["tasks"])
    rendered = json.dumps(artifact)
    assert "A date-range helper" not in rendered
    assert str(ROOT) not in rendered
    assert "api_base" not in rendered
    assert "final_answer" not in rendered


def test_live_condition_requires_a_matching_qualification_before_provider_resolution(tmp_path):
    brief_path = _write_json(tmp_path / "brief.json", _brief())
    with pytest.raises(ValueError, match="requires a matching qualification artifact"):
        run_condition(
            brief_path=brief_path,
            condition_name="candidate",
            tasks_path=TASKS,
            allow_dirty=True,
        )

    qualification = qualify_benchmark(TASKS)
    qualification["benchmark"]["corpus_digest"] = "sha256:" + "0" * 64
    qualification_path = _write_json(tmp_path / "qualification.json", qualification)
    with pytest.raises(ValueError, match="does not match current benchmark"):
        run_condition(
            brief_path=brief_path,
            condition_name="candidate",
            tasks_path=TASKS,
            model_client_factory=lambda **_kwargs: pytest.fail("provider must not run"),
            allow_dirty=True,
            qualification_path=qualification_path,
        )


def test_trial_rejects_fixture_identity_drift_before_starting_the_provider(tmp_path):
    task = load_tasks(TASKS)["tasks"][0]
    fixture = tmp_path / "fixture"
    shutil.copytree(task["fixture_path"], fixture)
    (fixture / "unexpected.py").write_text("value = 1\n", encoding="utf-8")
    task["fixture_path"] = fixture

    result = run_benchmark._run_trial(
        task,
        1,
        tmp_path / "runs",
        lambda **_kwargs: pytest.fail("provider must not run"),
        2048,
    )

    assert result == {
        "trial": 1,
        "status": "invalid",
        "reason": "fixture_identity_mismatch",
    }


def test_rejected_policy_attempt_is_diagnostic_not_a_hard_gate():
    report = {
        "run": {"stop_reason": "final_answer_returned"},
        "finalization": {"status": "complete", "error_count": 0},
    }
    trace = [
        {
            "tool_status": "rejected",
            "security_event_type": "trusted_executable_missing",
        },
        {
            "tool_status": "error",
            "security_event_type": "workspace_effect_unknown",
        },
    ]

    assert run_benchmark._policy_rejections(trace) == {
        "trusted_executable_missing": 1
    }
    assert run_benchmark._hard_gate_failures(report, trace, True) == [
        "workspace_effect_unknown"
    ]


def _comparison_artifacts(tmp_path):
    baseline_sha = "a" * 40
    candidate_sha = "b" * 40
    brief = _brief(baseline_sha, candidate_sha)
    brief_path = _write_json(tmp_path / "brief.json", brief)
    benchmark = {
        "source": "benchmarks/coding_quality/tasks.json",
        "corpus_digest": "sha256:" + "c" * 64,
        "grader_digest": "sha256:" + "d" * 64,
        "task_ids": ["diagnosis-date-boundary"],
    }
    provider = {
        "kind": "live",
        "provider": "openai-responses",
        "protocol": "openai_responses",
        "model": "model",
        "endpoint_hash": "sha256:" + "e" * 64,
        "resolution_source": "explicit",
        "candidate_count": 0,
        "probe_model_calls": 0,
        "usage_status": "not_checked",
    }
    protocol = {
        "trials_per_task": 1,
        "request_timeout_seconds": 90,
        "max_output_tokens": 2048,
        "permission_mode": "auto",
        "permission_rules": {"run_shell": "allow"},
        "dangerous_bypass": False,
    }

    def trial(scc):
        outcome = {
            "target_pass": scc,
            "regression_pass": True,
            "integrity_pass": True,
            "finalization_pass": True,
            "within_step_budget": True,
            "within_wall_budget": True,
            "hard_gate_failures": [],
        }
        return {
            "trial": 1,
            "status": "pass" if scc else "fail",
            "safe_correct_completion": scc,
            "outcome": outcome,
            "failure_category": None if scc else "target_behavior",
            "tool_steps": 2,
            "model_attempts": 1,
            "duration_ms": 10,
            "usage": None,
            "self_verification": scc,
            "successful_shell_checks_after_mutation": int(scc),
            "policy_rejections": {},
        }

    def artifact(name, sha, scc):
        row = {
            "id": "diagnosis-date-boundary",
            "slice": "failing-test-diagnosis",
            "trials": [trial(bool(scc))],
            "scc_count": int(scc),
        }
        return {
            "record_type": "coding_quality_condition_result",
            "format_version": 2,
            "captured_at": "2026-08-04T00:00:00+00:00",
            "brief_digest": run_benchmark._canonical_digest(brief),
            "condition": {
                "name": name,
                "label": name,
                "commit_sha": sha,
                "branch": f"codex/{name}",
                "dirty": False,
            },
            "benchmark": benchmark,
            "provider": provider,
            "protocol": protocol,
            "summary": run_benchmark._condition_summary([row]),
            "tasks": [row],
            "claim_scope": "live coding-quality pilot",
        }

    baseline_path = _write_json(tmp_path / "baseline.json", artifact("baseline", baseline_sha, 0))
    candidate_path = _write_json(tmp_path / "candidate.json", artifact("candidate", candidate_sha, 1))
    return brief_path, baseline_path, candidate_path


def test_comparator_maps_frozen_task_wins_to_accept(tmp_path):
    brief, baseline, candidate = _comparison_artifacts(tmp_path)

    comparison = compare_artifacts(
        brief_path=brief,
        baseline_path=baseline,
        candidate_path=candidate,
    )

    assert comparison["decision"] == "accept"
    assert comparison["evidence"]["task_wins"] == ["diagnosis-date-boundary"]
    assert comparison["action"].startswith("proceed")


def test_comparator_returns_inconclusive_when_frozen_provenance_differs(tmp_path):
    brief, baseline, candidate = _comparison_artifacts(tmp_path)
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["benchmark"]["grader_digest"] = "sha256:" + "f" * 64
    _write_json(candidate, payload)

    comparison = compare_artifacts(
        brief_path=brief,
        baseline_path=baseline,
        candidate_path=candidate,
    )

    assert comparison["decision"] == "inconclusive"
    assert comparison["evidence"]["frozen_condition_mismatches"] == ["benchmark"]


def test_comparator_honors_frozen_provider_failure_as_inconclusive(tmp_path):
    brief, baseline, candidate = _comparison_artifacts(tmp_path)
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    payload["tasks"][0]["trials"][0]["failure_category"] = "runtime_or_transport"
    payload["summary"] = run_benchmark._condition_summary(payload["tasks"])
    _write_json(baseline, payload)

    comparison = compare_artifacts(
        brief_path=brief,
        baseline_path=baseline,
        candidate_path=candidate,
    )

    assert comparison["decision"] == "inconclusive"
    assert comparison["evidence"]["frozen_condition_mismatches"] == [
        "provider_transport_failure"
    ]


def test_comparator_rejects_internally_inconsistent_condition_artifacts(tmp_path):
    brief, baseline, candidate = _comparison_artifacts(tmp_path)
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    payload["summary"]["safe_correct_completions"] = 0
    _write_json(candidate, payload)

    with pytest.raises(ValueError, match="summary does not match"):
        compare_artifacts(
            brief_path=brief,
            baseline_path=baseline,
            candidate_path=candidate,
        )


def test_comparator_does_not_treat_scripted_provider_plumbing_as_coding_evidence(tmp_path):
    brief, baseline, candidate = _comparison_artifacts(tmp_path)
    for path in (baseline, candidate):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["provider"].update(
            kind="scripted",
            provider="fake",
            protocol="scripted",
            model="FakeModelClient",
            endpoint_hash="",
            resolution_source="test_factory",
            usage_status="not_applicable",
        )
        payload["claim_scope"] = "non-confirmatory plumbing or dirty-worktree pilot"
        _write_json(path, payload)

    comparison = compare_artifacts(
        brief_path=brief,
        baseline_path=baseline,
        candidate_path=candidate,
    )

    assert comparison["decision"] == "inconclusive"
    assert comparison["evidence"]["frozen_condition_mismatches"] == [
        "claim_scope",
        "non_live_provider",
    ]
