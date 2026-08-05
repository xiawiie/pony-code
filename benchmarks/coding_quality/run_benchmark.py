#!/usr/bin/env python3
"""Qualify, run, and compare Pony coding-quality benchmark conditions."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pony.agent.observability import load_run_artifacts  # noqa: E402
from pony.config.environment import read_project_env  # noqa: E402
from pony.config.model import resolve_model_config  # noqa: E402
from pony.providers.factory import build_transport_client  # noqa: E402
from pony.providers.probe import resolve_provider_client  # noqa: E402
from pony.runtime.application import Pony  # noqa: E402
from pony.runtime.options import RuntimeOptions  # noqa: E402
from pony.state.run_store import RunStore  # noqa: E402
from pony.state.session_store import SessionStore  # noqa: E402
from pony.tools.registry import legal_tool_names  # noqa: E402
from pony.tools.subprocess import run_hardened_command, run_hardened_git  # noqa: E402
from pony.workspace.context import WorkspaceContext  # noqa: E402

TASKS_FORMAT_VERSION = 1
BRIEF_FORMAT_VERSION = 1
QUALIFICATION_FORMAT_VERSION = 1
CONDITION_FORMAT_VERSION = 3
COMPARISON_FORMAT_VERSION = 1
DEFAULT_TASKS_PATH = Path("benchmarks/coding_quality/tasks.json")
TASK_KEYS = {
    "id",
    "slice",
    "prompt",
    "fixture",
    "grader",
    "reference",
    "allowed_tools",
    "allowed_changes",
    "step_budget",
    "wall_time_seconds",
    "failure_mode",
}
BRIEF_KEYS = {
    "record_type",
    "format_version",
    "decision",
    "question",
    "hypothesis",
    "baseline",
    "candidate",
    "target_slices",
    "primary_metric",
    "expected_effect",
    "guardrails",
    "trials_per_task",
    "decision_rule",
    "inconclusive_conditions",
}
IGNORED_WORKSPACE_PARTS = {".pony", "__pycache__", ".pytest_cache"}
MAX_TASKS = 64
MAX_TREE_FILES = 256
MAX_FILE_BYTES = 512 * 1024
MAX_TREE_BYTES = 4 * 1024 * 1024
GRADER_TIMEOUT_SECONDS = 30
DEFAULT_REQUEST_TIMEOUT_SECONDS = 90
DEFAULT_MAX_OUTPUT_TOKENS = 2048
MUTATION_TOOLS = {"patch_file", "write_file"}
REQUIRED_INCONCLUSIVE_CONDITIONS = {
    "dirty_worktree",
    "frozen_condition_or_provenance_mismatch",
    "invalid_trial",
    "non_live_provider",
}
OPTIONAL_INCONCLUSIVE_CONDITIONS = {"provider_transport_failure"}


def _decode_json_object(path):
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    source = Path(path)
    _regular_file(source, "JSON record")
    payload = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    if not isinstance(payload, dict):
        raise ValueError("JSON record must be an object")
    return payload


def _require_exact_keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(f"invalid {label} fields")


def _bounded_text(value, label, *, max_length=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"invalid {label}")
    return value.strip()


def _relative_path(value, label):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"invalid {label}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"invalid {label}")
    return path


def _resolved_repo_path(repo_root, value, label):
    relative = _relative_path(value, label)
    root = Path(repo_root).resolve()
    path = (root / Path(*relative.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes repository") from exc
    return path


def _regular_file(path, label):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
        raise ValueError(f"unsafe {label}")
    return info


def _tree_snapshot(root, *, ignore_generated=False):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("unsafe benchmark tree")
    result = {}
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        relative = path.relative_to(root)
        if ignore_generated and any(part in IGNORED_WORKSPACE_PARTS for part in relative.parts):
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("benchmark tree contains unsafe entry")
        if info.st_size > MAX_FILE_BYTES:
            raise ValueError("benchmark file too large")
        total_bytes += info.st_size
        if len(result) >= MAX_TREE_FILES or total_bytes > MAX_TREE_BYTES:
            raise ValueError("benchmark tree too large")
        result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not result:
        raise ValueError("benchmark tree is empty")
    return result


def _canonical_digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_digest(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sha256_digest(value, label):
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"invalid {label}")
    return value


def _nonnegative_int(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid {label}")
    return value


def validate_tasks(payload, *, repo_root=ROOT):
    _require_exact_keys(payload, {"record_type", "format_version", "tasks"}, "benchmark")
    if payload["record_type"] != "coding_quality_benchmark" or payload["format_version"] != 1:
        raise ValueError("unsupported coding-quality benchmark")
    tasks = payload["tasks"]
    if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_TASKS:
        raise ValueError("benchmark tasks must be a bounded non-empty list")
    valid_tools = legal_tool_names()
    seen_ids = set()
    normalized = []
    for raw in tasks:
        _require_exact_keys(raw, TASK_KEYS, "task")
        task = dict(raw)
        task_id = _bounded_text(task["id"], "task id", max_length=100)
        if task_id in seen_ids or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in task_id):
            raise ValueError("invalid or duplicate task id")
        seen_ids.add(task_id)
        task_slice = _bounded_text(task["slice"], "task slice", max_length=100)
        prompt = _bounded_text(task["prompt"], "task prompt")
        failure_mode = _bounded_text(task["failure_mode"], "failure mode", max_length=500)
        fixture = _resolved_repo_path(repo_root, task["fixture"], "fixture path")
        reference = _resolved_repo_path(repo_root, task["reference"], "reference path")
        grader = _resolved_repo_path(repo_root, task["grader"], "grader path")
        fixture_snapshot = _tree_snapshot(fixture)
        reference_snapshot = _tree_snapshot(reference)
        _regular_file(grader, "grader")
        tools = task["allowed_tools"]
        if not isinstance(tools, list) or not tools or len(tools) != len(set(tools)):
            raise ValueError("allowed_tools must be a unique non-empty list")
        if any(not isinstance(tool, str) or tool not in valid_tools for tool in tools):
            raise ValueError("task contains unknown tool")
        changes = task["allowed_changes"]
        if not isinstance(changes, list) or not changes or len(changes) != len(set(changes)):
            raise ValueError("allowed_changes must be a unique non-empty list")
        normalized_changes = [_relative_path(item, "allowed change").as_posix() for item in changes]
        if not set(reference_snapshot) <= set(normalized_changes):
            raise ValueError("reference overlay escapes allowed_changes")
        step_budget = task["step_budget"]
        wall_time = task["wall_time_seconds"]
        if type(step_budget) is not int or not 1 <= step_budget <= 50:
            raise ValueError("invalid step budget")
        if type(wall_time) is not int or not 1 <= wall_time <= 1800:
            raise ValueError("invalid wall-time budget")
        task.update(
            id=task_id,
            slice=task_slice,
            prompt=prompt,
            failure_mode=failure_mode,
            allowed_tools=list(tools),
            allowed_changes=normalized_changes,
            fixture_path=fixture,
            reference_path=reference,
            grader_path=grader,
            fixture_snapshot=fixture_snapshot,
            reference_snapshot=reference_snapshot,
        )
        normalized.append(task)
    return {"record_type": payload["record_type"], "format_version": 1, "tasks": normalized}


def _benchmark_manifest(path, repo_root):
    root = Path(repo_root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    _regular_file(candidate, "benchmark manifest")
    resolved = candidate.resolve()
    try:
        source = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("benchmark manifest must be inside repository") from exc
    return resolved, source


def load_tasks(path=DEFAULT_TASKS_PATH, *, repo_root=ROOT):
    manifest, _source = _benchmark_manifest(path, repo_root)
    return validate_tasks(_decode_json_object(manifest), repo_root=repo_root)


def validate_evaluation_brief(payload):
    _require_exact_keys(payload, BRIEF_KEYS, "evaluation brief")
    if payload["record_type"] != "evaluation_brief" or payload["format_version"] != BRIEF_FORMAT_VERSION:
        raise ValueError("unsupported evaluation brief")
    for key in ("decision", "question", "hypothesis", "decision_rule"):
        _bounded_text(payload[key], key)
    for condition_name in ("baseline", "candidate"):
        condition = payload[condition_name]
        _require_exact_keys(condition, {"label", "commit_sha"}, condition_name)
        _bounded_text(condition["label"], f"{condition_name} label", max_length=100)
        sha = condition["commit_sha"]
        if not isinstance(sha, str) or len(sha) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in sha
        ):
            raise ValueError(f"invalid {condition_name} commit")
    if payload["baseline"]["label"] == payload["candidate"]["label"]:
        raise ValueError("condition labels must differ")
    slices = payload["target_slices"]
    if not isinstance(slices, list) or not slices or len(slices) != len(set(slices)):
        raise ValueError("target_slices must be a unique non-empty list")
    for value in slices:
        _bounded_text(value, "target slice", max_length=100)
    if payload["primary_metric"] != "safe_correct_completion":
        raise ValueError("unsupported primary metric")
    _require_exact_keys(payload["expected_effect"], {"min_task_wins", "max_task_losses"}, "expected effect")
    for key, value in payload["expected_effect"].items():
        if type(value) is not int or value < 0:
            raise ValueError(f"invalid expected effect {key}")
    _require_exact_keys(
        payload["guardrails"],
        {"forbid_stable_pass_to_fail", "max_hard_gate_failures"},
        "guardrails",
    )
    if type(payload["guardrails"]["forbid_stable_pass_to_fail"]) is not bool:
        raise ValueError("invalid stable-pass guardrail")
    if type(payload["guardrails"]["max_hard_gate_failures"]) is not int or payload["guardrails"]["max_hard_gate_failures"] < 0:
        raise ValueError("invalid hard-gate guardrail")
    trials = payload["trials_per_task"]
    if type(trials) is not int or not 1 <= trials <= 20:
        raise ValueError("invalid trials_per_task")
    conditions = payload["inconclusive_conditions"]
    if (
        not isinstance(conditions, list)
        or len(conditions) != len(set(conditions))
        or not REQUIRED_INCONCLUSIVE_CONDITIONS.issubset(conditions)
        or not set(conditions).issubset(
            REQUIRED_INCONCLUSIVE_CONDITIONS | OPTIONAL_INCONCLUSIVE_CONDITIONS
        )
    ):
        raise ValueError("invalid inconclusive_conditions")
    return payload


def load_evaluation_brief(path):
    return validate_evaluation_brief(_decode_json_object(path))


def _grader_python():
    candidate = Path("/usr/bin/python3")
    if not candidate.exists():
        raise ValueError("trusted grader Python unavailable")
    return str(candidate)


def _grader_env():
    return {
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }


def _run_grader(task, workspace, mode):
    return run_hardened_command(
        _grader_python(),
        args=(task["grader_path"], task["id"], mode),
        cwd=workspace,
        timeout=GRADER_TIMEOUT_SECONDS,
        env=_grader_env(),
    )


def _apply_reference(task, workspace):
    for relative in task["reference_snapshot"]:
        source = task["reference_path"] / relative
        target = Path(workspace) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _integrity_result(before, after, allowed_changes):
    changed = sorted(
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    )
    forbidden = [key for key in changed if key not in set(allowed_changes)]
    return not forbidden, changed, forbidden


def _benchmark_digests(benchmark):
    public_tasks = []
    grader_paths = set()
    for task in benchmark["tasks"]:
        public_tasks.append({key: task[key] for key in TASK_KEYS})
        public_tasks[-1]["fixture_snapshot"] = task["fixture_snapshot"]
        public_tasks[-1]["reference_snapshot"] = task["reference_snapshot"]
        grader_paths.add(task["grader_path"])
    graders = {str(path.relative_to(ROOT)): _file_digest(path) for path in sorted(grader_paths)}
    return _canonical_digest(public_tasks), _canonical_digest(graders)


def qualify_benchmark(tasks_path=DEFAULT_TASKS_PATH, *, output_path=None, repo_root=ROOT):
    benchmark = load_tasks(tasks_path, repo_root=repo_root)
    corpus_digest, grader_digest = _benchmark_digests(benchmark)
    rows = []
    with tempfile.TemporaryDirectory(prefix="pony-coding-quality-qualification-") as directory:
        root = Path(directory)
        for task in benchmark["tasks"]:
            workspace = root / task["id"]
            shutil.copytree(task["fixture_path"], workspace)
            broken_target = [_run_grader(task, workspace, "target").returncode for _ in range(3)]
            broken_regression = _run_grader(task, workspace, "regression").returncode
            broken_public = _run_grader(task, workspace, "public").returncode
            broken_public_expected = (
                "fail" if task["slice"] == "failing-test-diagnosis" else "pass"
            )
            _apply_reference(task, workspace)
            reference_runs = [
                (
                    _run_grader(task, workspace, "target").returncode,
                    _run_grader(task, workspace, "regression").returncode,
                    _run_grader(task, workspace, "public").returncode,
                )
                for _ in range(5)
            ]
            reference_snapshot = _tree_snapshot(workspace, ignore_generated=True)
            integrity_pass, _, _ = _integrity_result(
                task["fixture_snapshot"], reference_snapshot, task["allowed_changes"]
            )
            tampered = dict(reference_snapshot)
            tampered["hidden-grader-copy.py"] = "tampered"
            tamper_rejected = not _integrity_result(
                task["fixture_snapshot"], tampered, task["allowed_changes"]
            )[0]
            qualified = (
                all(code != 0 for code in broken_target)
                and broken_regression == 0
                and ("pass" if broken_public == 0 else "fail") == broken_public_expected
                and reference_runs == [(0, 0, 0)] * 5
                and integrity_pass
                and tamper_rejected
            )
            rows.append(
                {
                    "id": task["id"],
                    "slice": task["slice"],
                    "qualified": qualified,
                    "broken_target_failures": sum(code != 0 for code in broken_target),
                    "broken_regression_pass": broken_regression == 0,
                    "broken_public_expected": broken_public_expected,
                    "broken_public_observed": "pass" if broken_public == 0 else "fail",
                    "reference_passes": sum(pair == (0, 0, 0) for pair in reference_runs),
                    "integrity_pass": integrity_pass,
                    "tamper_rejected": tamper_rejected,
                }
            )
    artifact = {
        "record_type": "coding_quality_qualification",
        "format_version": QUALIFICATION_FORMAT_VERSION,
        "captured_at": _captured_at(),
        "benchmark": {
            "task_count": len(rows),
            "corpus_digest": corpus_digest,
            "grader_digest": grader_digest,
        },
        "summary": {
            "qualified": sum(row["qualified"] for row in rows),
            "failed": sum(not row["qualified"] for row in rows),
        },
        "tasks": rows,
        "claim_scope": "offline benchmark and grader qualification only",
    }
    if output_path is not None:
        _write_json_atomic(output_path, artifact)
    return artifact


def validate_qualification_artifact(payload, benchmark):
    _require_exact_keys(
        payload,
        {
            "record_type",
            "format_version",
            "captured_at",
            "benchmark",
            "summary",
            "tasks",
            "claim_scope",
        },
        "qualification artifact",
    )
    if (
        payload["record_type"] != "coding_quality_qualification"
        or payload["format_version"] != QUALIFICATION_FORMAT_VERSION
    ):
        raise ValueError("unsupported qualification artifact")
    _bounded_text(payload["captured_at"], "qualification captured_at", max_length=100)
    if payload["claim_scope"] != "offline benchmark and grader qualification only":
        raise ValueError("invalid qualification claim scope")
    corpus_digest, grader_digest = _benchmark_digests(benchmark)
    _require_exact_keys(
        payload["benchmark"], {"task_count", "corpus_digest", "grader_digest"}, "qualification benchmark"
    )
    expected_benchmark = {
        "task_count": len(benchmark["tasks"]),
        "corpus_digest": corpus_digest,
        "grader_digest": grader_digest,
    }
    if payload["benchmark"] != expected_benchmark:
        raise ValueError("qualification artifact does not match current benchmark")
    rows = payload["tasks"]
    if not isinstance(rows, list) or len(rows) != len(benchmark["tasks"]):
        raise ValueError("qualification task count mismatch")
    expected_rows = {task["id"]: task for task in benchmark["tasks"]}
    seen = set()
    qualified_count = 0
    for row in rows:
        _require_exact_keys(
            row,
            {
                "id",
                "slice",
                "qualified",
                "broken_target_failures",
                "broken_regression_pass",
                "broken_public_expected",
                "broken_public_observed",
                "reference_passes",
                "integrity_pass",
                "tamper_rejected",
            },
            "qualification task",
        )
        row_id = row["id"]
        if not isinstance(row_id, str):
            raise ValueError("qualification task identity mismatch")
        task = expected_rows.get(row_id)
        if task is None or row_id in seen or row["slice"] != task["slice"]:
            raise ValueError("qualification task identity mismatch")
        seen.add(row_id)
        expected_public = "fail" if task["slice"] == "failing-test-diagnosis" else "pass"
        evidence_pass = (
            row["broken_target_failures"] == 3
            and row["broken_regression_pass"] is True
            and row["broken_public_expected"] == expected_public
            and row["broken_public_observed"] == expected_public
            and row["reference_passes"] == 5
            and row["integrity_pass"] is True
            and row["tamper_rejected"] is True
        )
        if type(row["qualified"]) is not bool or row["qualified"] != evidence_pass:
            raise ValueError("qualification task evidence mismatch")
        qualified_count += row["qualified"]
    _require_exact_keys(payload["summary"], {"qualified", "failed"}, "qualification summary")
    expected_summary = {
        "qualified": qualified_count,
        "failed": len(rows) - qualified_count,
    }
    if payload["summary"] != expected_summary or expected_summary["failed"] != 0:
        raise ValueError("benchmark qualification failed")
    return payload


def load_qualification_artifact(path, benchmark):
    return validate_qualification_artifact(_decode_json_object(path), benchmark)


def _captured_at():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(args, *, cwd):
    result = run_hardened_git("/usr/bin/git", args, cwd=cwd, text=True, check=True, timeout=10)
    return result.stdout.strip()


def _git_provenance(repo_root):
    return {
        "commit_sha": _git(["rev-parse", "HEAD"], cwd=repo_root),
        "branch": _git(["branch", "--show-current"], cwd=repo_root),
        "dirty": bool(_git(["status", "--short"], cwd=repo_root)),
    }


def _provider_target(repo_root, request_timeout_seconds):
    project_env = read_project_env(repo_root, warn=False)
    config = resolve_model_config(project_env=project_env, required=True)
    _client, resolved, report = resolve_provider_client(
        config,
        timeout=request_timeout_seconds,
    )

    def factory(**_kwargs):
        return build_transport_client(
            resolved["protocol"]["value"],
            model=resolved["model"]["value"],
            base_url=resolved["base_url"]["value"],
            api_key=resolved["api_key"]["value"],
            timeout=request_timeout_seconds,
            auth_mode=resolved["auth_mode"]["value"],
            capabilities=resolved["capabilities"],
            temperature=0.0,
        )

    summary = {
        "kind": "live",
        "provider": resolved["resolved_provider"]["value"],
        "protocol": resolved["protocol"]["value"],
        "model": resolved["model"]["value"],
        "endpoint_hash": "sha256:" + hashlib.sha256(
            resolved["base_url"]["value"].encode("utf-8")
        ).hexdigest(),
        "resolution_source": resolved["resolution_source"],
        "candidate_count": int(report.get("candidate_count", 0) or 0),
        "probe_model_calls": int(report.get("model_calls", 0) or 0),
        "usage_status": str(report.get("usage_status", "not_checked") or "not_checked"),
    }
    return factory, summary


def _fake_provider_target(factory):
    return factory, {
        "kind": "scripted",
        "provider": "fake",
        "protocol": "scripted",
        "model": "FakeModelClient",
        "endpoint_hash": "",
        "resolution_source": "test_factory",
        "candidate_count": 0,
        "probe_model_calls": 0,
        "usage_status": "not_applicable",
    }


@contextmanager
def _wall_budget(seconds):
    if not hasattr(signal, "setitimer"):
        yield
        return
    previous = signal.getsignal(signal.SIGALRM)

    def timeout_handler(_signum, _frame):
        raise TimeoutError("coding benchmark wall-time budget exceeded")

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _hard_gate_failures(report, trace, integrity_pass):
    failures = []
    if not integrity_pass:
        failures.append("scope_integrity")
    if report["run"]["stop_reason"] == "persistence_error":
        failures.append("persistence")
    if report["finalization"]["status"] != "complete" or report["finalization"]["error_count"] != 0:
        failures.append("finalization")
    for event in trace:
        security = str(event.get("security_event_type", "") or "")
        if security and event.get("tool_status") != "rejected":
            failures.append(security)
    return sorted(set(failures))


def _policy_rejections(trace):
    result = {}
    for event in trace:
        security = str(event.get("security_event_type", "") or "")
        if security and event.get("tool_status") == "rejected":
            result[security] = result.get(security, 0) + 1
    return dict(sorted(result.items()))


def _self_verification(trace):
    last_mutation = -1
    for index, event in enumerate(trace):
        if event.get("event") == "tool_executed" and event.get("name") in MUTATION_TOOLS:
            last_mutation = index
    checks = sum(
        event.get("event") == "tool_executed"
        and event.get("name") == "run_shell"
        and event.get("tool_status") == "ok"
        for event in trace[last_mutation + 1 :]
    )
    return last_mutation >= 0 and checks > 0, checks


def _failure_category(outcome, report):
    if outcome["hard_gate_failures"]:
        return "hard_gate"
    if not outcome["target_pass"]:
        return "target_behavior"
    if not outcome["regression_pass"]:
        return "hidden_regression"
    if not outcome["finalization_pass"]:
        return "persistence_finalization"
    if not outcome["within_step_budget"] or not outcome["within_wall_budget"]:
        return "budget"
    stop_reason = report["run"]["stop_reason"]
    if stop_reason in {"model_error", "retry_limit_reached"}:
        return "provider_transport"
    return "agent_completion"


def _product_failure(task, trial_number, category, started):
    return {
        "trial": trial_number,
        "status": "fail",
        "safe_correct_completion": False,
        "outcome": {
            "target_pass": False,
            "regression_pass": False,
            "integrity_pass": False,
            "finalization_pass": False,
            "within_step_budget": False,
            "within_wall_budget": time.monotonic() - started <= task["wall_time_seconds"],
            "hard_gate_failures": [],
        },
        "failure_category": category,
        "tool_steps": 0,
        "model_attempts": 0,
        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
        "usage": None,
        "self_verification": False,
        "successful_shell_checks_after_mutation": 0,
        "policy_rejections": {},
        "scope": None,
    }


def _run_trial(task, trial_number, workspace_root, model_client_factory, max_output_tokens):
    started = time.monotonic()
    workspace = Path(workspace_root) / f"{task['id']}-trial-{trial_number}"
    try:
        shutil.copytree(task["fixture_path"], workspace)
        before = _tree_snapshot(workspace, ignore_generated=True)
    except Exception:
        return {"trial": trial_number, "status": "invalid", "reason": "fixture_copy_error"}
    if before != task["fixture_snapshot"]:
        return {"trial": trial_number, "status": "invalid", "reason": "fixture_identity_mismatch"}
    context = WorkspaceContext.build(workspace, repo_root_override=workspace)
    run_store = RunStore(workspace / ".pony" / "runs")
    session_store = SessionStore(workspace / ".pony" / "sessions")
    client = model_client_factory(task=task, workspace=context, trial=trial_number)
    agent = Pony(
        model_client=client,
        workspace=context,
        session_store=session_store,
        options=RuntimeOptions(
            run_store=run_store,
            project_trusted=True,
            max_steps=task["step_budget"],
            max_output_tokens=max_output_tokens,
            allowed_tools=tuple(task["allowed_tools"]),
        ),
    )
    if "run_shell" in task["allowed_tools"]:
        agent.set_permission_rule("run_shell", "allow")
    try:
        with _wall_budget(task["wall_time_seconds"]):
            agent.ask(task["prompt"])
    except TimeoutError:
        return _product_failure(task, trial_number, "budget", started)
    except Exception:
        return _product_failure(task, trial_number, "runtime_or_transport", started)
    try:
        report, trace = load_run_artifacts(run_store.root, agent.current_task_state.run_id)
        target = _run_grader(task, workspace, "target")
        regression = _run_grader(task, workspace, "regression")
        after = _tree_snapshot(workspace, ignore_generated=True)
    except Exception:
        return {
            "trial": trial_number,
            "status": "invalid",
            "reason": "runner_or_grader_error",
        }
    integrity_pass, changed, forbidden = _integrity_result(
        before, after, task["allowed_changes"]
    )
    wall_time = time.monotonic() - started
    finalization_pass = (
        report["run"]["status"] == "completed"
        and report["run"]["stop_reason"] == "final_answer_returned"
        and report["finalization"]["status"] == "complete"
        and report["finalization"]["error_count"] == 0
        and report["integrity"]["terminal_event_expected"] is True
    )
    hard_gates = _hard_gate_failures(report, trace, integrity_pass)
    policy_rejections = _policy_rejections(trace)
    outcome = {
        "target_pass": target.returncode == 0,
        "regression_pass": regression.returncode == 0,
        "integrity_pass": integrity_pass,
        "finalization_pass": finalization_pass,
        "within_step_budget": report["tools"]["calls"] <= task["step_budget"],
        "within_wall_budget": wall_time <= task["wall_time_seconds"],
        "hard_gate_failures": hard_gates,
    }
    scc = all(
        outcome[key]
        for key in (
            "target_pass",
            "regression_pass",
            "integrity_pass",
            "finalization_pass",
            "within_step_budget",
            "within_wall_budget",
        )
    ) and not hard_gates
    verified, shell_checks = _self_verification(trace)
    usage = report["model"].get("usage")
    return {
        "trial": trial_number,
        "status": "pass" if scc else "fail",
        "safe_correct_completion": scc,
        "outcome": outcome,
        "failure_category": None if scc else _failure_category(outcome, report),
        "tool_steps": report["tools"]["calls"],
        "model_attempts": report["model"]["attempts"],
        "duration_ms": report["run"]["duration_ms"],
        "usage": usage if isinstance(usage, dict) else None,
        "self_verification": verified,
        "successful_shell_checks_after_mutation": shell_checks,
        "policy_rejections": policy_rejections,
        "scope": {"changed_files": changed, "forbidden_files": forbidden},
    }


def _select_tasks(benchmark, brief):
    selected = [task for task in benchmark["tasks"] if task["slice"] in brief["target_slices"]]
    found = {task["slice"] for task in selected}
    if found != set(brief["target_slices"]):
        raise ValueError("evaluation brief references an unknown or empty task slice")
    return selected


def _condition_summary(task_rows):
    trials = [trial for row in task_rows for trial in row["trials"]]
    valid = [trial for trial in trials if trial["status"] != "invalid"]
    categories = {}
    policy_rejections = {}
    hard_gates = 0
    for trial in valid:
        category = trial.get("failure_category")
        if category:
            categories[category] = categories.get(category, 0) + 1
        hard_gates += len(trial["outcome"]["hard_gate_failures"])
        for code, count in trial["policy_rejections"].items():
            policy_rejections[code] = policy_rejections.get(code, 0) + count
    return {
        "tasks": len(task_rows),
        "trials": len(trials),
        "invalid_trials": len(trials) - len(valid),
        "safe_correct_completions": sum(trial["safe_correct_completion"] for trial in valid),
        "hard_gate_failures": hard_gates,
        "self_verified_trials": sum(trial["self_verification"] for trial in valid),
        "failure_categories": categories,
        "policy_rejections": dict(sorted(policy_rejections.items())),
    }


def run_condition(
    *,
    brief_path,
    condition_name,
    output_path=None,
    tasks_path=DEFAULT_TASKS_PATH,
    repo_root=ROOT,
    provider_repo_root=None,
    workspace_root=None,
    request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
    max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
    model_client_factory=None,
    allow_dirty=False,
    qualification_path=None,
):
    brief = load_evaluation_brief(brief_path)
    if condition_name not in {"baseline", "candidate"}:
        raise ValueError("condition_name must be baseline or candidate")
    benchmark = load_tasks(tasks_path, repo_root=repo_root)
    _manifest, benchmark_source = _benchmark_manifest(tasks_path, repo_root)
    tasks = _select_tasks(benchmark, brief)
    corpus_digest, grader_digest = _benchmark_digests(benchmark)
    if qualification_path is not None:
        load_qualification_artifact(qualification_path, benchmark)
    elif model_client_factory is None:
        raise ValueError("live coding benchmark requires a matching qualification artifact")
    provenance = _git_provenance(repo_root)
    expected_condition = brief[condition_name]
    if provenance["commit_sha"] != expected_condition["commit_sha"]:
        raise ValueError("condition commit does not match current exact HEAD")
    if provenance["dirty"] and not allow_dirty:
        raise ValueError("confirmatory condition requires a clean worktree")
    if type(request_timeout_seconds) is not int or not 1 <= request_timeout_seconds <= 300:
        raise ValueError("invalid request timeout")
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8192:
        raise ValueError("invalid max output tokens")
    if model_client_factory is None:
        client_factory, provider = _provider_target(
            Path(provider_repo_root or repo_root), request_timeout_seconds
        )
    else:
        client_factory, provider = _fake_provider_target(model_client_factory)
    owned_workspace = workspace_root is None
    root = Path(
        tempfile.mkdtemp(prefix="pony-coding-quality-run-")
        if owned_workspace
        else workspace_root
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        rows = []
        for task in tasks:
            trials = [
                _run_trial(task, index, root, client_factory, max_output_tokens)
                for index in range(1, brief["trials_per_task"] + 1)
            ]
            rows.append(
                {
                    "id": task["id"],
                    "slice": task["slice"],
                    "trials": trials,
                    "scc_count": sum(
                        trial.get("safe_correct_completion", False) for trial in trials
                    ),
                }
            )
    finally:
        if owned_workspace:
            shutil.rmtree(root, ignore_errors=True)
    artifact = {
        "record_type": "coding_quality_condition_result",
        "format_version": CONDITION_FORMAT_VERSION,
        "captured_at": _captured_at(),
        "brief_digest": _canonical_digest(brief),
        "condition": {
            "name": condition_name,
            "label": expected_condition["label"],
            "commit_sha": provenance["commit_sha"],
            "branch": provenance["branch"],
            "dirty": provenance["dirty"],
        },
        "benchmark": {
            "source": benchmark_source,
            "corpus_digest": corpus_digest,
            "grader_digest": grader_digest,
            "task_ids": [task["id"] for task in tasks],
        },
        "provider": provider,
        "protocol": {
            "trials_per_task": brief["trials_per_task"],
            "request_timeout_seconds": request_timeout_seconds,
            "max_output_tokens": max_output_tokens,
            "permission_mode": "auto",
            "permission_rules": {"run_shell": "allow"},
            "dangerous_bypass": False,
        },
        "summary": _condition_summary(rows),
        "tasks": rows,
        "claim_scope": (
            "live coding-quality pilot"
            if provider["kind"] == "live" and not provenance["dirty"]
            else "non-confirmatory plumbing or dirty-worktree pilot"
        ),
    }
    if output_path is not None:
        _write_json_atomic(output_path, artifact)
    return artifact


def _validate_condition_artifact(payload):
    _require_exact_keys(
        payload,
        {
            "record_type",
            "format_version",
            "captured_at",
            "brief_digest",
            "condition",
            "benchmark",
            "provider",
            "protocol",
            "summary",
            "tasks",
            "claim_scope",
        },
        "condition artifact",
    )
    if (
        payload["record_type"] != "coding_quality_condition_result"
        or payload["format_version"] != CONDITION_FORMAT_VERSION
    ):
        raise ValueError("unsupported condition artifact")
    _bounded_text(payload["captured_at"], "condition captured_at", max_length=100)
    _sha256_digest(payload["brief_digest"], "brief digest")
    if payload["claim_scope"] not in {
        "live coding-quality pilot",
        "non-confirmatory plumbing or dirty-worktree pilot",
    }:
        raise ValueError("invalid condition claim scope")

    condition = payload["condition"]
    _require_exact_keys(
        condition, {"name", "label", "commit_sha", "branch", "dirty"}, "condition identity"
    )
    if condition["name"] not in {"baseline", "candidate"}:
        raise ValueError("invalid condition name")
    _bounded_text(condition["label"], "condition label", max_length=100)
    sha = condition["commit_sha"]
    if not isinstance(sha, str) or len(sha) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in sha
    ):
        raise ValueError("invalid condition commit")
    if not isinstance(condition["branch"], str) or len(condition["branch"]) > 200:
        raise ValueError("invalid condition branch")
    if type(condition["dirty"]) is not bool:
        raise ValueError("invalid condition dirty state")

    benchmark = payload["benchmark"]
    _require_exact_keys(
        benchmark, {"source", "corpus_digest", "grader_digest", "task_ids"}, "condition benchmark"
    )
    _relative_path(benchmark["source"], "benchmark source")
    _sha256_digest(benchmark["corpus_digest"], "corpus digest")
    _sha256_digest(benchmark["grader_digest"], "grader digest")
    task_ids = benchmark["task_ids"]
    if (
        not isinstance(task_ids, list)
        or not task_ids
        or any(not isinstance(task_id, str) or not task_id for task_id in task_ids)
        or len(task_ids) != len(set(task_ids))
    ):
        raise ValueError("invalid benchmark task ids")

    provider = payload["provider"]
    _require_exact_keys(
        provider,
        {
            "kind",
            "provider",
            "protocol",
            "model",
            "endpoint_hash",
            "resolution_source",
            "candidate_count",
            "probe_model_calls",
            "usage_status",
        },
        "condition provider",
    )
    if provider["kind"] not in {"live", "scripted"}:
        raise ValueError("invalid provider kind")
    for key in ("provider", "protocol", "model", "resolution_source", "usage_status"):
        _bounded_text(provider[key], f"provider {key}", max_length=300)
    if provider["kind"] == "live":
        _sha256_digest(provider["endpoint_hash"], "provider endpoint hash")
    elif provider["endpoint_hash"] != "":
        raise ValueError("scripted provider must not have an endpoint hash")
    _nonnegative_int(provider["candidate_count"], "provider candidate count")
    _nonnegative_int(provider["probe_model_calls"], "provider probe calls")

    protocol = payload["protocol"]
    _require_exact_keys(
        protocol,
        {
            "trials_per_task",
            "request_timeout_seconds",
            "max_output_tokens",
            "permission_mode",
            "permission_rules",
            "dangerous_bypass",
        },
        "condition protocol",
    )
    trials_per_task = protocol["trials_per_task"]
    if type(trials_per_task) is not int or not 1 <= trials_per_task <= 20:
        raise ValueError("invalid condition trial count")
    if (
        type(protocol["request_timeout_seconds"]) is not int
        or not 1 <= protocol["request_timeout_seconds"] <= 300
    ):
        raise ValueError("invalid condition request timeout")
    if (
        type(protocol["max_output_tokens"]) is not int
        or not 1 <= protocol["max_output_tokens"] <= 8192
    ):
        raise ValueError("invalid condition max output tokens")
    if protocol["permission_mode"] != "auto":
        raise ValueError("invalid condition permission mode")
    if protocol["permission_rules"] != {"run_shell": "allow"}:
        raise ValueError("invalid condition permission rules")
    if protocol["dangerous_bypass"] is not False:
        raise ValueError("dangerous bypass is not allowed")

    rows = payload["tasks"]
    if not isinstance(rows, list) or len(rows) != len(task_ids):
        raise ValueError("condition artifact task count mismatch")
    for row, expected_id in zip(rows, task_ids, strict=True):
        _require_exact_keys(row, {"id", "slice", "trials", "scc_count"}, "condition task")
        if row["id"] != expected_id:
            raise ValueError("condition task identity mismatch")
        _bounded_text(row["slice"], "condition task slice", max_length=100)
        trials = row["trials"]
        if not isinstance(trials, list) or len(trials) != trials_per_task:
            raise ValueError("condition task trial count mismatch")
        scc_count = 0
        for expected_trial, trial in enumerate(trials, start=1):
            if not isinstance(trial, dict) or trial.get("trial") != expected_trial:
                raise ValueError("condition trial identity mismatch")
            if trial.get("status") == "invalid":
                _require_exact_keys(trial, {"trial", "status", "reason"}, "invalid trial")
                _bounded_text(trial["reason"], "invalid trial reason", max_length=100)
                continue
            _require_exact_keys(
                trial,
                {
                    "trial",
                    "status",
                    "safe_correct_completion",
                    "outcome",
                    "failure_category",
                    "tool_steps",
                    "model_attempts",
                    "duration_ms",
                    "usage",
                    "self_verification",
                    "successful_shell_checks_after_mutation",
                    "policy_rejections",
                    "scope",
                },
                "condition trial",
            )
            if trial["status"] not in {"pass", "fail"}:
                raise ValueError("invalid trial status")
            if type(trial["safe_correct_completion"]) is not bool:
                raise ValueError("invalid trial SCC state")
            if (trial["status"] == "pass") != trial["safe_correct_completion"]:
                raise ValueError("trial status disagrees with SCC state")
            outcome = trial["outcome"]
            _require_exact_keys(
                outcome,
                {
                    "target_pass",
                    "regression_pass",
                    "integrity_pass",
                    "finalization_pass",
                    "within_step_budget",
                    "within_wall_budget",
                    "hard_gate_failures",
                },
                "trial outcome",
            )
            for key in (
                "target_pass",
                "regression_pass",
                "integrity_pass",
                "finalization_pass",
                "within_step_budget",
                "within_wall_budget",
            ):
                if type(outcome[key]) is not bool:
                    raise ValueError(f"invalid trial outcome {key}")
            hard_gates = outcome["hard_gate_failures"]
            if (
                not isinstance(hard_gates, list)
                or any(not isinstance(value, str) or not value for value in hard_gates)
                or len(hard_gates) != len(set(hard_gates))
            ):
                raise ValueError("invalid trial hard-gate failures")
            computed_scc = all(
                outcome[key]
                for key in (
                    "target_pass",
                    "regression_pass",
                    "integrity_pass",
                    "finalization_pass",
                    "within_step_budget",
                    "within_wall_budget",
                )
            ) and not hard_gates
            if computed_scc != trial["safe_correct_completion"]:
                raise ValueError("trial outcome disagrees with SCC state")
            if computed_scc:
                if trial["failure_category"] is not None:
                    raise ValueError("successful trial has a failure category")
                scc_count += 1
            else:
                _bounded_text(trial["failure_category"], "trial failure category", max_length=100)
            _nonnegative_int(trial["tool_steps"], "trial tool steps")
            _nonnegative_int(trial["model_attempts"], "trial model attempts")
            _nonnegative_int(trial["duration_ms"], "trial duration")
            if trial["usage"] is not None and not isinstance(trial["usage"], dict):
                raise ValueError("invalid trial usage")
            if type(trial["self_verification"]) is not bool:
                raise ValueError("invalid trial self-verification state")
            _nonnegative_int(
                trial["successful_shell_checks_after_mutation"], "trial shell checks"
            )
            scope = trial["scope"]
            if scope is None:
                if outcome["integrity_pass"]:
                    raise ValueError("passing integrity requires scope evidence")
            else:
                _require_exact_keys(
                    scope, {"changed_files", "forbidden_files"}, "trial scope evidence"
                )
                changed_files = scope["changed_files"]
                forbidden_files = scope["forbidden_files"]
                for values, label in (
                    (changed_files, "changed files"),
                    (forbidden_files, "forbidden files"),
                ):
                    if (
                        not isinstance(values, list)
                        or len(values) > MAX_TREE_FILES
                        or values != sorted(set(values))
                    ):
                        raise ValueError(f"invalid trial scope {label}")
                    for value in values:
                        _relative_path(value, f"trial scope {label}")
                if not set(forbidden_files).issubset(changed_files):
                    raise ValueError("trial forbidden files must be changed files")
                if outcome["integrity_pass"] != (not forbidden_files):
                    raise ValueError("trial scope disagrees with integrity state")
            policy_rejections = trial["policy_rejections"]
            if not isinstance(policy_rejections, dict):
                raise ValueError("invalid trial policy rejections")
            for code, count in policy_rejections.items():
                _bounded_text(code, "trial policy rejection", max_length=100)
                if type(count) is not int or count < 1:
                    raise ValueError("invalid trial policy rejection count")
        if row["scc_count"] != scc_count:
            raise ValueError("condition task SCC count mismatch")

    _require_exact_keys(
        payload["summary"],
        {
            "tasks",
            "trials",
            "invalid_trials",
            "safe_correct_completions",
            "hard_gate_failures",
            "self_verified_trials",
            "failure_categories",
            "policy_rejections",
        },
        "condition summary",
    )
    if payload["summary"] != _condition_summary(rows):
        raise ValueError("condition summary does not match task trials")
    return payload


def compare_artifacts(*, brief_path, baseline_path, candidate_path, output_path=None):
    brief = load_evaluation_brief(brief_path)
    baseline = _validate_condition_artifact(_decode_json_object(baseline_path))
    candidate = _validate_condition_artifact(_decode_json_object(candidate_path))
    mismatches = []
    for key in ("brief_digest", "benchmark", "provider", "protocol"):
        if baseline[key] != candidate[key]:
            mismatches.append(key)
    if baseline["brief_digest"] != _canonical_digest(brief):
        mismatches.append("brief")
    if baseline["condition"]["name"] != "baseline":
        mismatches.append("baseline_condition")
    if candidate["condition"]["name"] != "candidate":
        mismatches.append("candidate_condition")
    if baseline["condition"]["label"] != brief["baseline"]["label"]:
        mismatches.append("baseline_label")
    if candidate["condition"]["label"] != brief["candidate"]["label"]:
        mismatches.append("candidate_label")
    if baseline["condition"]["commit_sha"] != brief["baseline"]["commit_sha"]:
        mismatches.append("baseline_commit")
    if candidate["condition"]["commit_sha"] != brief["candidate"]["commit_sha"]:
        mismatches.append("candidate_commit")
    if baseline["condition"]["dirty"] or candidate["condition"]["dirty"]:
        mismatches.append("dirty_worktree")
    if baseline["provider"]["kind"] != "live" or candidate["provider"]["kind"] != "live":
        mismatches.append("non_live_provider")
    if (
        baseline["claim_scope"] != "live coding-quality pilot"
        or candidate["claim_scope"] != "live coding-quality pilot"
    ):
        mismatches.append("claim_scope")
    if baseline["summary"]["invalid_trials"] or candidate["summary"]["invalid_trials"]:
        mismatches.append("invalid_trials")
    if "provider_transport_failure" in brief["inconclusive_conditions"] and any(
        trial.get("failure_category") in {"provider_transport", "runtime_or_transport"}
        for condition in (baseline, candidate)
        for task in condition["tasks"]
        for trial in task["trials"]
    ):
        mismatches.append("provider_transport_failure")
    baseline_slices = {row["slice"] for row in baseline["tasks"]}
    candidate_slices = {row["slice"] for row in candidate["tasks"]}
    if baseline_slices != set(brief["target_slices"]) or candidate_slices != baseline_slices:
        mismatches.append("target_slices")
    baseline_tasks = {row["id"]: row for row in baseline["tasks"]}
    candidate_tasks = {row["id"]: row for row in candidate["tasks"]}
    if set(baseline_tasks) != set(candidate_tasks):
        mismatches.append("task_set")
    wins = []
    losses = []
    ties = []
    stable_pass_to_fail = []
    if not mismatches:
        trials = brief["trials_per_task"]
        for task_id in baseline["benchmark"]["task_ids"]:
            before = baseline_tasks[task_id]["scc_count"]
            after = candidate_tasks[task_id]["scc_count"]
            if after > before:
                wins.append(task_id)
            elif after < before:
                losses.append(task_id)
            else:
                ties.append(task_id)
            if before == trials and after < trials:
                stable_pass_to_fail.append(task_id)
    guardrails = []
    if candidate["summary"]["hard_gate_failures"] > brief["guardrails"]["max_hard_gate_failures"]:
        guardrails.append("hard_gate_failures")
    if brief["guardrails"]["forbid_stable_pass_to_fail"] and stable_pass_to_fail:
        guardrails.append("stable_pass_to_fail")
    expected = brief["expected_effect"]
    effect_pass = len(wins) >= expected["min_task_wins"] and len(losses) <= expected["max_task_losses"]
    decision = "inconclusive" if mismatches else "accept" if effect_pass and not guardrails else "reject"
    artifact = {
        "record_type": "coding_quality_comparison",
        "format_version": COMPARISON_FORMAT_VERSION,
        "captured_at": _captured_at(),
        "decision": decision,
        "reason": (
            "frozen conditions or provenance differ"
            if mismatches
            else "expected effect and guardrails passed"
            if decision == "accept"
            else "expected effect or guardrail failed"
        ),
        "evidence": {
            "task_wins": wins,
            "task_losses": losses,
            "task_ties": ties,
            "stable_pass_to_fail": stable_pass_to_fail,
            "guardrail_failures": guardrails,
            "frozen_condition_mismatches": sorted(set(mismatches)),
        },
        "action": {
            "accept": "proceed with the decision named in the frozen brief",
            "reject": "do not claim improvement; inspect task-level failures",
            "inconclusive": "rerun with matching clean frozen conditions",
        }[decision],
    }
    if output_path is not None:
        _write_json_atomic(output_path, artifact)
    return artifact


def _write_json_atomic(path, payload):
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    qualify = subparsers.add_parser("qualify")
    qualify.add_argument("--tasks", type=Path, default=DEFAULT_TASKS_PATH)
    qualify.add_argument("--output", type=Path, required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--brief", type=Path, required=True)
    run.add_argument("--condition", choices=("baseline", "candidate"), required=True)
    run.add_argument("--tasks", type=Path, default=DEFAULT_TASKS_PATH)
    run.add_argument("--qualification", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--repo-root", type=Path, default=ROOT)
    run.add_argument("--provider-repo-root", type=Path, default=None)
    run.add_argument("--allow-dirty", action="store_true")
    compare = subparsers.add_parser("compare")
    compare.add_argument("--brief", type=Path, required=True)
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if args.command == "qualify":
        artifact = qualify_benchmark(args.tasks, output_path=args.output)
        print(json.dumps(artifact["summary"], sort_keys=True))
        return 0 if artifact["summary"]["failed"] == 0 else 1
    if args.command == "run":
        artifact = run_condition(
            brief_path=args.brief,
            condition_name=args.condition,
            output_path=args.output,
            tasks_path=args.tasks,
            repo_root=args.repo_root,
            provider_repo_root=args.provider_repo_root,
            allow_dirty=args.allow_dirty,
            qualification_path=args.qualification,
        )
        print(json.dumps(artifact["summary"], sort_keys=True))
        return 0 if artifact["summary"]["invalid_trials"] == 0 else 1
    artifact = compare_artifacts(
        brief_path=args.brief,
        baseline_path=args.baseline,
        candidate_path=args.candidate,
        output_path=args.output,
    )
    print(json.dumps({"decision": artifact["decision"], "reason": artifact["reason"]}))
    return 0 if artifact["decision"] == "accept" else 1


if __name__ == "__main__":
    raise SystemExit(main())
