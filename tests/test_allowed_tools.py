import json

import pytest

from pico import Pico, SessionStore, WorkspaceContext
from pico import tools as toolkit
from pico.evaluation.benchmark_schema import validate_benchmark
from pico.evaluation.fixed_benchmark import BenchmarkEvaluator
from pico.providers.fake import FakeModelClient


def build_agent(tmp_path, allowed_tools=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(["Done."]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        allowed_tools=allowed_tools,
    )


def test_allowed_tools_filter_prompt_and_reject_direct_execution(tmp_path):
    agent = build_agent(tmp_path, allowed_tools=["read_file"])

    prompt = agent.prefix

    assert "- read_file(" in prompt
    assert "- run_shell(" not in prompt
    assert agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20}) == "error: tool 'run_shell' is not allowed in this run"


def test_allowed_tools_reject_unknown_tool_at_construction(tmp_path):
    with pytest.raises(ValueError, match="unknown allowed tool"):
        build_agent(tmp_path, allowed_tools=["read_file", "missing_tool"])


def test_validate_benchmark_rejects_unknown_allowed_tool(tmp_path):
    fixture = tmp_path / "bench_repo_readme"
    fixture.mkdir()
    (fixture / "README.md").write_text("demo\n", encoding="utf-8")

    benchmark = {
        "record_type": "fixed_benchmark_definition",
        "format_version": 1,
        "tasks": [
            {
                "id": "bad_allowed_tool",
                "prompt": "Inspect README.",
                "fixture_repo": "bench_repo_readme",
                "allowed_tools": ["read_file", "missing_tool"],
                "step_budget": 1,
                "expected_artifact": "README.md",
                "verifier": "python -c 'print(1)'",
                "category": "contract",
            }
        ],
    }

    with pytest.raises(ValueError, match="unknown allowed_tools entry"):
        validate_benchmark(benchmark, repo_root=tmp_path)


def test_benchmark_evaluator_applies_allowed_tools_to_runtime_prompt(tmp_path):
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
                        "id": "prompt_allowlist",
                        "prompt": "Inspect README.",
                        "fixture_repo": "bench_repo_readme",
                        "allowed_tools": ["read_file"],
                        "step_budget": 1,
                        "expected_artifact": "README.md",
                        "verifier": "python -c 'import pathlib; assert pathlib.Path(\"README.md\").exists()'",
                        "category": "contract",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    captured_clients = []

    class CaptureModelClient(FakeModelClient):
        def __init__(self):
            super().__init__(["Done."])
            captured_clients.append(self)

    evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        workspace_root=tmp_path / "workspaces",
        model_client_factory=lambda task, workspace: CaptureModelClient(),
    )

    row = evaluator.run_task(evaluator.load()["tasks"][0])

    assert row["status"] == "pass"
    tool_names = [tool["name"] for tool in captured_clients[0].requests[0]["tools"]]
    assert "read_file" in tool_names
    assert "run_shell" not in tool_names


def test_allowed_tools_filter_prompt_examples_and_rules(tmp_path):
    agent = build_agent(tmp_path, allowed_tools=["read_file"])

    prompt = agent.prefix

    assert "- read_file(" in prompt
    assert "- write_file(" not in prompt
    assert "run_shell" not in prompt


def test_allowed_tools_filter_file_edit_rules_to_available_tools(tmp_path):
    agent = build_agent(tmp_path, allowed_tools=["patch_file"])

    prompt = agent.prefix

    assert "- patch_file(" in prompt
    assert "- write_file(" not in prompt
    assert "use patch_file" in prompt
    assert "use write_file" not in prompt


def test_allowed_tools_prompt_includes_search_example_and_required_args(tmp_path):
    agent = build_agent(tmp_path, allowed_tools=["search"])

    prompt = agent.prefix

    assert "- search(" in prompt
    assert '"name":"search"' in agent.tool_example("search")
    assert "Do not call search with args={}" in prompt


def test_prompt_examples_use_tools_module_as_single_source(tmp_path, monkeypatch):
    monkeypatch.setitem(
        toolkit.TOOL_EXAMPLES,
        "search",
        '{"name":"search","arguments":{"pattern":"SINGLE_SOURCE","path":"."}}',
    )
    agent = build_agent(tmp_path, allowed_tools=["search"])

    prompt = agent.tool_example("search")

    assert "SINGLE_SOURCE" in prompt
