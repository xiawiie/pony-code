import json
from pathlib import Path
from collections import Counter

import pytest

from benchmarks.evaluation.benchmark_schema import (
    load_benchmark,
    summarize_rows,
)
from benchmarks.evaluation.fixed_benchmark import (
    BenchmarkEvaluator,
    _verifier_argv,
    run_fixed_benchmark,
    run_harness_regression_v2,
)
from pico.agent.observability import RunArtifactError
from pico.providers.fake import FakeModelClient


def test_load_benchmark_validates_fixed_schema():
    benchmark = load_benchmark(Path("benchmarks/coding_tasks.json"))

    assert benchmark["record_type"] == "fixed_benchmark_definition"
    assert benchmark["format_version"] == 1
    assert len(benchmark["tasks"]) == 10
    assert Counter(task["category"] for task in benchmark["tasks"]) == {
        "documentation": 2,
        "text-edit": 2,
        "tool-boundary": 3,
        "recovery": 3,
    }
    for task in benchmark["tasks"]:
        assert {"id", "prompt", "fixture_repo", "allowed_tools", "step_budget", "expected_artifact", "verifier", "category"} <= set(task)
        assert isinstance(task["allowed_tools"], list)
        assert task["step_budget"] > 0


def test_load_benchmark_rejects_missing_required_task_fields(tmp_path):
    benchmark_path = tmp_path / "bad-benchmark.json"
    benchmark_path.write_text(
        json.dumps(
            {
                "record_type": "fixed_benchmark_definition",
                "format_version": 1,
                "tasks": [
                    {
                        "id": "broken",
                        "prompt": "Missing required task keys.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="required"):
        load_benchmark(benchmark_path)


@pytest.mark.parametrize("version", [None, True, 1.0, "1", 2])
def test_load_benchmark_rejects_noncurrent_format_before_tasks(tmp_path, version):
    payload = {
        "record_type": "fixed_benchmark_definition",
        "format_version": version,
        "tasks": "poisoned-business-shape",
    }
    if version is None:
        payload.pop("format_version")
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="format_version"):
        load_benchmark(path)


def test_load_benchmark_rejects_wrong_type_and_nested_duplicate_keys(tmp_path):
    wrong_type = tmp_path / "wrong-type.json"
    wrong_type.write_text(
        json.dumps(
            {
                "record_type": "fixed_benchmark_result",
                "format_version": 1,
                "tasks": "poisoned-business-shape",
            }
        ),
        encoding="utf-8",
    )
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"record_type":"fixed_benchmark_definition","format_version":1,'
        '"tasks":[],"nested":{"key":1,"key":2}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="record_type"):
        load_benchmark(wrong_type)
    with pytest.raises(ValueError, match="duplicate"):
        load_benchmark(duplicate)


def test_verifier_is_parsed_as_structured_argv_without_shell_operators():
    assert _verifier_argv("python3 -c 'print(1)'") == [
        "python3",
        "-c",
        "print(1)",
    ]
    with pytest.raises(ValueError, match="shell operators"):
        _verifier_argv("python3 -c 'print(1)' && touch escaped")


def test_run_fixed_benchmark_uses_fresh_fixture_copy_and_fresh_run_directory(tmp_path):
    artifact_path = tmp_path / "benchmark-v1.json"
    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=artifact_path,
        workspace_root=tmp_path / "workspaces",
    )

    original_fixture = Path("tests/fixtures/bench_repo_patch/sample.txt").read_text(encoding="utf-8")
    artifact = evaluator.run()

    row = next(item for item in artifact["rows"] if item["id"] == "sample_beta_locked")
    copied_fixture = (tmp_path / "workspaces" / row["fixture_copy_relpath"]).resolve()
    run_dir = (tmp_path / "workspaces" / row["run_dir_relpath"]).resolve()

    assert artifact_path.exists()
    assert copied_fixture.exists()
    assert run_dir.exists()
    assert not row["fixture_copy_relpath"].startswith("/")
    assert not row["run_dir_relpath"].startswith("/")
    assert row["initial_messages_empty"] is True
    assert row["message_invariants_valid"] is True
    assert "initial_history_empty" not in row
    assert row["initial_memory_empty"] is True
    assert row["initial_task_summary_empty"] is True
    assert Path("tests/fixtures/bench_repo_patch/sample.txt").read_text(encoding="utf-8") == original_fixture
    assert "beta-locked" in (copied_fixture / "sample.txt").read_text(encoding="utf-8")


def test_run_fixed_benchmark_reports_metadata_and_success_definition(tmp_path):
    artifact_path = tmp_path / "benchmark-v1.json"
    artifact = run_fixed_benchmark(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=artifact_path,
        workspace_root=tmp_path / "workspaces",
    )

    assert artifact_path.exists()
    persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert persisted == artifact

    assert artifact["record_type"] == "fixed_benchmark_result"
    assert artifact["format_version"] == 1
    assert artifact["summary"] == {
        "total_tasks": 10,
        "passed": 10,
        "failed": 0,
        "pass_rate": 1.0,
        "within_budget": 10,
        "verifier_passes": 10,
        "within_budget_rate": 1.0,
        "verifier_pass_rate": 1.0,
        "failure_category_counts": {},
    }
    assert artifact["failure_category_counts"] == {}

    reproducibility = artifact["reproducibility"]
    assert reproducibility["model_name"] == "FakeModelClient"
    assert reproducibility["model_version"] == "scripted-deterministic"
    assert reproducibility["fixture_snapshot_id"].startswith("sha256:")
    assert reproducibility["decoding"] == {
        "temperature": 0.0,
        "top_p": 1.0,
        "max_output_tokens": 64,
    }
    assert reproducibility["timezone"] == "Asia/Shanghai"
    assert reproducibility["locale"] == "C.UTF-8"

    for row in artifact["rows"]:
        assert not row["fixture_copy_relpath"].startswith("/")
        assert not row["run_dir_relpath"].startswith("/")
        assert not row["task_state_relpath"].startswith("/")
        assert not row["report_relpath"].startswith("/")
        assert row["status"] == "pass"
        assert row["passed"] is True
        assert row["within_budget"] is True
        assert row["verifier_passed"] is True
        assert row["expected_artifact_exists"] is True
        assert row["non_failure_stop_reason"] is True
        assert row["stop_reason"] == "final_answer_returned"


def test_run_task_rejects_missing_run_artifact(tmp_path, monkeypatch):
    from pico.runtime import Pico

    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "benchmark-v1.json",
        workspace_root=tmp_path / "workspaces",
    )
    task = next(item for item in evaluator.load()["tasks"] if item["id"] == "readme_intro_locked")
    real_ask = Pico.ask

    def remove_task_state_after_ask(self, user_message):
        result = real_ask(self, user_message)
        self.run_store.task_state_path(self.current_task_state).unlink()
        return result

    monkeypatch.setattr(Pico, "ask", remove_task_state_after_ask)

    with pytest.raises(RunArtifactError, match="missing"):
        evaluator.run_task(task)


def test_failure_category_enum_is_stable():
    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=Path("artifacts/test-failure-category.json"),
        workspace_root=Path("artifacts/test-failure-category-workspaces"),
    )

    cases = [
        (True, True, False, True, "missing_artifact"),
        (False, True, True, True, "budget_exceeded"),
        (True, False, True, True, "verifier_failed"),
        (True, True, True, False, "failure_stop_reason"),
        (True, True, True, True, "unknown"),
    ]
    for (
        within_budget,
        verifier_passed,
        expected_artifact_exists,
        non_failure_stop_reason,
        expected,
    ) in cases:
        assert (
            evaluator._failure_category(
                within_budget,
                verifier_passed,
                expected_artifact_exists,
                non_failure_stop_reason,
            )
            == expected
        )


def test_benchmark_reproducibility_locale_is_stable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "benchmarks.evaluation.fixed_benchmark.locale_module.setlocale",
        lambda category: "zh_CN.UTF-8",
    )

    artifact = run_fixed_benchmark(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "benchmark-v1.json",
        workspace_root=tmp_path / "workspaces",
    )

    assert artifact["reproducibility"]["locale"] == "C.UTF-8"


def test_benchmark_verifier_runs_with_reproducibility_locale(monkeypatch, tmp_path):
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    fixture = tmp_path / "bench_repo_readme"
    fixture.mkdir()
    (fixture / "README.md").write_text("demo\n", encoding="utf-8")
    benchmark_dir = tmp_path / "benchmarks"
    benchmark_dir.mkdir()
    benchmark_path = benchmark_dir / "benchmark.json"
    benchmark_path.write_text(
        json.dumps(
            {
                "record_type": "fixed_benchmark_definition",
                "format_version": 1,
                "tasks": [
                    {
                        "id": "locale_env",
                        "prompt": "Create the locale artifact.",
                        "fixture_repo": "bench_repo_readme",
                        "allowed_tools": ["read_file"],
                        "step_budget": 1,
                        "expected_artifact": "README.md",
                        "verifier": (
                            "python -c 'import os, pathlib; "
                            "pathlib.Path(\"verifier-env.txt\").write_text("
                            "os.environ.get(\"LC_ALL\", \"\") + \"\\n\" + os.environ.get(\"LANG\", \"\")); "
                            "assert os.environ.get(\"LC_ALL\") == \"C.UTF-8\"; "
                            "assert os.environ.get(\"LANG\") == \"C.UTF-8\"'"
                        ),
                        "category": "contract",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        model_client_factory=lambda task, workspace: FakeModelClient(["done"]),
    )

    row = evaluator.run_task(evaluator.load()["tasks"][0])

    assert row["status"] == "pass"
    verifier_env = tmp_path / "workspaces" / row["fixture_copy_relpath"] / "verifier-env.txt"
    assert verifier_env.read_text(encoding="utf-8").splitlines() == ["C.UTF-8", "C.UTF-8"]


def test_real_provider_benchmark_prompt_includes_success_criteria(tmp_path):
    fixture = tmp_path / "bench_repo_readme"
    fixture.mkdir()
    (fixture / "README.md").write_text("demo\n", encoding="utf-8")
    benchmark_dir = tmp_path / "benchmarks"
    benchmark_dir.mkdir()
    benchmark_path = benchmark_dir / "benchmark.json"
    benchmark_path.write_text(
        json.dumps(
            {
                "record_type": "fixed_benchmark_definition",
                "format_version": 1,
                "tasks": [
                    {
                        "id": "criteria_prompt",
                        "prompt": "Update the README.",
                        "fixture_repo": "bench_repo_readme",
                        "allowed_tools": ["read_file"],
                        "step_budget": 1,
                        "expected_artifact": "README.md contains benchmark success text",
                        "verifier": "python -c 'from pathlib import Path; assert Path(\"README.md\").exists()'",
                        "category": "contract",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    clients = []

    def factory(task, workspace):
        del task, workspace
        client = FakeModelClient(["done"])
        clients.append(client)
        return client

    evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        model_client_factory=factory,
    )

    row = evaluator.run_task(evaluator.load()["tasks"][0])

    assert row["status"] == "pass"
    prompt = clients[0].requests[0]["messages"][-1]["content"]
    assert "Success criteria:" in prompt
    assert "README.md contains benchmark success text" in prompt
    assert "Verification command:" in prompt
    assert "Path(\"README.md\").exists()" in prompt
    assert "Do not run the verification command yourself" in prompt


def test_run_fixed_benchmark_covers_recovery_rows(tmp_path):
    artifact = run_fixed_benchmark(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=tmp_path / "benchmark-v1.json",
        workspace_root=tmp_path / "workspaces",
    )

    context_row = next(
        item
        for item in artifact["rows"]
        if item["id"] == "session_compaction_checkpoint"
    )

    trace_path = (tmp_path / "workspaces" / context_row["run_dir_relpath"] / "trace.jsonl").resolve()
    trace_events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]

    assert any(
        event.get("event") == "checkpoint_created"
        and event.get("trigger") == "run_finished"
        for event in trace_events
    )
    session_path = next(
        (tmp_path / "workspaces" / context_row["fixture_copy_relpath"] / ".pico" / "sessions").glob("*.jsonl")
    )
    session_entries = [
        json.loads(line)
        for line in session_path.read_text(encoding="utf-8").splitlines()[1:]
    ]
    assert any(
        entry.get("type") == "compaction"
        and entry.get("data", {}).get("reason") == "benchmark_setup"
        for entry in session_entries
    )


def test_run_harness_regression_v2_writes_named_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "harness-regression-v2.json"

    artifact = run_harness_regression_v2(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=artifact_path,
        workspace_root=tmp_path / "workspaces",
    )

    assert artifact_path.exists()
    assert artifact["summary"]["total_tasks"] == 10
    assert artifact["summary"]["pass_rate"] == 1.0
    assert artifact["summary"]["within_budget_rate"] == 1.0
    assert artifact["summary"]["verifier_pass_rate"] == 1.0


def test_run_task_anchors_paths_to_fixture_copy_inside_workspace_root(tmp_path):
    workspace_root = tmp_path / "workspace"
    evaluator = BenchmarkEvaluator(
        benchmark_path=Path("benchmarks/coding_tasks.json"),
        artifact_path=workspace_root / "benchmark-v1.json",
        workspace_root=workspace_root,
    )

    task = next(item for item in evaluator.load()["tasks"] if item["id"] == "readme_intro_locked")
    row = evaluator.run_task(task)

    assert row["status"] == "pass"
    fixture_copy = workspace_root / row["fixture_copy_relpath"]
    readme_path = fixture_copy / "README.md"
    assert "This fixture is a locked benchmark workspace." in readme_path.read_text(encoding="utf-8")


def test_summarize_rows_counts_failure_categories():
    summary = summarize_rows(
        [
            {
                "status": "pass",
                "within_budget": True,
                "verifier_passed": True,
                "expected_artifact_exists": True,
                "non_failure_stop_reason": True,
            },
            {
                "status": "fail",
                "within_budget": False,
                "verifier_passed": False,
                "expected_artifact_exists": False,
                "non_failure_stop_reason": False,
                "failure_category": "verifier_failed",
            },
            {
                "status": "fail",
                "within_budget": False,
                "verifier_passed": True,
                "expected_artifact_exists": True,
                "non_failure_stop_reason": False,
                "failure_category": "budget_exceeded",
            },
        ]
    )

    assert summary["total_tasks"] == 3
    assert summary["passed"] == 1
    assert summary["failed"] == 2
    assert summary["pass_rate"] == pytest.approx(1 / 3)
    assert summary["within_budget"] == 1
    assert summary["verifier_passes"] == 2
    assert summary["failure_category_counts"] == {
        "budget_exceeded": 1,
        "verifier_failed": 1,
    }


def test_default_benchmark_workspace_resolves_temp_symlink(tmp_path, monkeypatch):
    import benchmarks.evaluation.fixed_benchmark as fixed_benchmark

    real_root = tmp_path / "real-temp"
    real_root.mkdir()
    linked_root = tmp_path / "linked-temp"
    linked_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setattr(
        fixed_benchmark.tempfile,
        "mkdtemp",
        lambda **kwargs: str(linked_root),
    )

    evaluator = fixed_benchmark.BenchmarkEvaluator()

    assert evaluator.workspace_root == real_root.resolve()
