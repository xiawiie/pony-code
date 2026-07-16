import hashlib
from pathlib import Path

from pico.tools.registry import legal_tool_names
from .metrics_common import _decode_json_object, _validate_record_header

FIXED_BENCHMARK_DEFINITION_FORMAT_VERSION = 1
DEFAULT_BENCHMARK_PATH = Path("benchmarks/coding_tasks.json")

REQUIRED_BENCHMARK_KEYS = ("record_type", "format_version", "tasks")
REQUIRED_TASK_KEYS = (
    "id",
    "prompt",
    "fixture_repo",
    "allowed_tools",
    "step_budget",
    "expected_artifact",
    "verifier",
    "category",
)

TASK_FIXTURE_ARTIFACTS = {
    "bench_repo_readme": "README.md",
    "bench_repo_patch": "sample.txt",
}

SCRIPTED_MODEL_OUTPUTS = {
    "readme_intro_locked": [
        {
            "name": "patch_file",
            "args": {
                "path": "README.md",
                "old_text": "This is a placeholder benchmark fixture.",
                "new_text": "This fixture is a locked benchmark workspace.",
            },
        },
        "Done.",
    ],
    "readme_schema_note": [
        {
            "name": "patch_file",
            "args": {
                "path": "README.md",
                "old_text": "- Placeholder note about the repo.",
                "new_text": "- The benchmark schema and baseline are fixed.",
            },
        },
        "Done.",
    ],
    "readme_ordering_note": [
        {
            "name": "patch_file",
            "args": {
                "path": "README.md",
                "old_text": "- Placeholder note about the file layout.",
                "new_text": "- Deterministic file ordering keeps benchmark diffs stable.",
            },
        },
        "Done.",
    ],
    "sample_beta_locked": [
        {
            "name": "patch_file",
            "args": {
                "path": "sample.txt",
                "old_text": "beta",
                "new_text": "beta-locked",
            },
        },
        "Done.",
    ],
    "sample_gamma_locked": [
        {
            "name": "patch_file",
            "args": {
                "path": "sample.txt",
                "old_text": "gamma",
                "new_text": "gamma-locked",
            },
        },
        "Done.",
    ],
    "sample_placeholder_delta": [
        {
            "name": "patch_file",
            "args": {
                "path": "sample.txt",
                "old_text": "placeholder",
                "new_text": "delta",
            },
        },
        "Done.",
    ],
    "invalid_patch_recovery": [
        {
            "name": "patch_file",
            "args": {
                "path": "README.md",
                "old_text": "This is a placeholder benchmark fixture.",
            },
        },
        {
            "name": "patch_file",
            "args": {
                "path": "README.md",
                "old_text": "This is a placeholder benchmark fixture.",
                "new_text": "This fixture recovered after invalid patch args.",
            },
        },
        "Done.",
    ],
    "path_escape_recovery": [
        {"name": "read_file", "args": {"path": "../outside.txt", "start": 1, "end": 1}},
        {
            "name": "patch_file",
            "args": {
                "path": "sample.txt",
                "old_text": "alpha",
                "new_text": "alpha-guarded",
            },
        },
        "Done.",
    ],
    "repeated_read_recovery": [
        {"name": "read_file", "args": {"path": "sample.txt", "start": 1, "end": 4}},
        {"name": "read_file", "args": {"path": "sample.txt", "start": 1, "end": 4}},
        {"name": "read_file", "args": {"path": "sample.txt", "start": 1, "end": 4}},
        {
            "name": "patch_file",
            "args": {
                "path": "sample.txt",
                "old_text": "placeholder",
                "new_text": "repeat-guarded",
            },
        },
        "Done.",
    ],
    "session_compaction_checkpoint": [
        "# Goal\nContinue the fixed benchmark after compaction.\n"
        "# Constraints & Preferences\nPreserve the active tail.\n"
        "# Progress\n## Done\nOlder benchmark history was summarized.\n"
        "## In Progress\nFinish the current benchmark task.\n"
        "## Blocked\nNone.\n# Key Decisions\nUse Session Tree compaction.\n"
        "# Next Steps\nReturn the benchmark result.\n"
        "# Critical Context\nREADME.md remains available.\n"
        "# Files & Errors\nREADME.md; no errors.",
        "Done.",
    ],
    "freshness_reanchor_resume": [
        "Done.",
    ],
    "workspace_mismatch_resume": [
        "Done.",
    ],
}


def _artifact_path_for_task(task):
    fixture_repo_name = Path(str(task["fixture_repo"])).name
    if fixture_repo_name not in TASK_FIXTURE_ARTIFACTS:
        raise ValueError(
            f"unsupported fixture repo for artifact lookup: {fixture_repo_name}"
        )
    return TASK_FIXTURE_ARTIFACTS[fixture_repo_name]


def _workspace_relative(path, workspace_root):
    return str(Path(path).resolve().relative_to(Path(workspace_root).resolve()))


def _scripted_outputs_for_task(task):
    outputs = SCRIPTED_MODEL_OUTPUTS.get(task["id"])
    if outputs is None:
        raise ValueError(f"no scripted model outputs for benchmark task: {task['id']}")
    return list(outputs)


def _fixture_snapshot_id(fixture_paths):
    sha = hashlib.sha256()
    for fixture_path in sorted(
        {Path(path).resolve() for path in fixture_paths}, key=lambda path: str(path)
    ):
        for path in sorted(
            (item for item in fixture_path.rglob("*") if item.is_file()),
            key=lambda item: str(item.relative_to(fixture_path)),
        ):
            sha.update(str(fixture_path.name).encode("utf-8"))
            sha.update(b"\0")
            sha.update(str(path.relative_to(fixture_path)).encode("utf-8"))
            sha.update(b"\0")
            sha.update(path.read_bytes())
            sha.update(b"\0")
    return "sha256:" + sha.hexdigest()


def validate_benchmark(data, repo_root=None):
    if not isinstance(data, dict):
        raise ValueError("benchmark must be a mapping")

    _validate_record_header(
        data,
        "fixed_benchmark_definition",
        FIXED_BENCHMARK_DEFINITION_FORMAT_VERSION,
    )

    missing = [key for key in REQUIRED_BENCHMARK_KEYS if key not in data]
    if missing:
        raise ValueError(f"benchmark is missing required keys: {', '.join(missing)}")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("benchmark tasks must be a non-empty list")

    repo_root = Path(repo_root or Path.cwd()).resolve()
    seen_ids = set()
    normalized_tasks = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"benchmark task at index {index} must be a mapping")

        missing_task_keys = [key for key in REQUIRED_TASK_KEYS if key not in task]
        if missing_task_keys:
            raise ValueError(
                f"benchmark task {task.get('id', index)!r} is missing required keys: {', '.join(missing_task_keys)}"
            )

        task_id = str(task["id"]).strip()
        if not task_id:
            raise ValueError(f"benchmark task at index {index} has an empty id")
        if task_id in seen_ids:
            raise ValueError(f"duplicate benchmark task id: {task_id}")
        seen_ids.add(task_id)

        fixture_repo = repo_root / str(task["fixture_repo"])
        if not fixture_repo.is_dir():
            raise ValueError(
                f"benchmark task {task_id} fixture repo does not exist: {task['fixture_repo']}"
            )

        allowed_tools = task["allowed_tools"]
        if not isinstance(allowed_tools, list) or not allowed_tools:
            raise ValueError(
                f"benchmark task {task_id} allowed_tools must be a non-empty list"
            )
        valid_tools = legal_tool_names()
        normalized_allowed_tools = []
        for tool in allowed_tools:
            tool_name = str(tool).strip()
            if not tool_name:
                raise ValueError(
                    f"benchmark task {task_id} has an empty allowed_tools entry"
                )
            if tool_name not in valid_tools:
                raise ValueError(
                    f"benchmark task {task_id} has an unknown allowed_tools entry: {tool_name}"
                )
            normalized_allowed_tools.append(tool_name)

        step_budget = int(task["step_budget"])
        if step_budget < 1:
            raise ValueError(f"benchmark task {task_id} step_budget must be positive")

        normalized_task = dict(task)
        normalized_task["id"] = task_id
        normalized_task["prompt"] = str(task["prompt"]).strip()
        normalized_task["fixture_repo"] = str(task["fixture_repo"]).strip()
        normalized_task["allowed_tools"] = normalized_allowed_tools
        normalized_task["step_budget"] = step_budget
        normalized_task["expected_artifact"] = str(task["expected_artifact"]).strip()
        normalized_task["verifier"] = str(task["verifier"]).strip()
        normalized_task["category"] = str(task["category"]).strip()
        normalized_tasks.append(normalized_task)

    normalized = dict(data)
    normalized["record_type"] = "fixed_benchmark_definition"
    normalized["format_version"] = FIXED_BENCHMARK_DEFINITION_FORMAT_VERSION
    normalized["tasks"] = normalized_tasks
    return normalized


def load_benchmark(path=DEFAULT_BENCHMARK_PATH, repo_root=None):
    path = Path(path)
    data = _decode_json_object(path.read_text(encoding="utf-8"))
    if repo_root is None:
        repo_root = path.resolve().parent.parent
    return validate_benchmark(data, repo_root=repo_root)


def summarize_rows(rows):
    rows = list(rows)
    passed = sum(1 for row in rows if row.get("passed") or row.get("status") == "pass")
    failed = len(rows) - passed
    failure_category_counts = {}
    for row in rows:
        if row.get("passed") or row.get("status") == "pass":
            continue
        category = str(row.get("failure_category") or "unknown")
        failure_category_counts[category] = failure_category_counts.get(category, 0) + 1

    total_tasks = len(rows)
    within_budget = sum(1 for row in rows if row.get("within_budget"))
    verifier_passes = sum(1 for row in rows if row.get("verifier_passed"))
    return {
        "total_tasks": total_tasks,
        "passed": passed,
        "failed": failed,
        "pass_rate": (passed / total_tasks) if total_tasks else 0.0,
        "within_budget": within_budget,
        "verifier_passes": verifier_passes,
        "within_budget_rate": (within_budget / total_tasks) if total_tasks else 0.0,
        "verifier_pass_rate": (verifier_passes / total_tasks) if total_tasks else 0.0,
        "failure_category_counts": failure_category_counts,
    }


def _digest_file(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
