import json
from types import SimpleNamespace

import pytest

from pony import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from pony.tools import registry as toolkit
from benchmarks.evaluation.benchmark_schema import validate_benchmark
from benchmarks.evaluation.fixed_benchmark import BenchmarkEvaluator
from benchmarks.support.fake_provider import FakeModelClient
from pony.runtime.options import RuntimeOptions


def build_agent(tmp_path, allowed_tools=None, *, executables=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path, executables=executables)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    return Pony(
        model_client=FakeModelClient(["Done."]),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True, allowed_tools=allowed_tools),
    )


def test_allowed_tools_filter_prompt_and_reject_direct_execution(tmp_path):
    agent = build_agent(tmp_path, allowed_tools=["read_file"])

    schemas = agent.visible_tools()

    assert set(schemas) == {"read_file"}
    assert (
        agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})
        == "error: tool 'run_shell' is not allowed in this run"
    )


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

    assert set(agent.visible_tools()) == {"read_file"}


def test_allowed_tools_filter_file_edit_rules_to_available_tools(tmp_path):
    agent = build_agent(tmp_path, allowed_tools=["patch_file"])

    assert set(agent.visible_tools()) == {"patch_file"}


def test_run_shell_schema_lists_safe_executable_names_without_paths(tmp_path):
    agent = build_agent(
        tmp_path,
        executables={"git": "/usr/bin/git", "python3": "/usr/bin/python3"},
    )

    description = agent.visible_tools()["run_shell"]["description"]

    assert "Available trusted executable names: git, python3." in description
    assert "/usr/bin" not in description


def test_run_shell_schema_uses_verified_sandbox_image_tools():
    context = SimpleNamespace(
        depth=0,
        max_depth=1,
        docker_sandbox=True,
        trusted_executables={"python3": "/usr/bin/python3"},
        sandbox_context=SimpleNamespace(
            runner=SimpleNamespace(
                image=SimpleNamespace(
                    tool_paths=(("pytest", "/usr/bin/pytest"), ("python", "/usr/bin/python"))
                )
            )
        ),
    )

    description = toolkit.build_tool_registry(context)["run_shell"]["description"]

    assert "Available trusted executable names: pytest, python." in description
    assert "python3" not in description
    assert "/usr/bin" not in description


def test_allowed_tools_prompt_includes_search_example_and_required_args(tmp_path):
    agent = build_agent(tmp_path, allowed_tools=["search"])

    assert set(agent.visible_tools()) == {"search"}
    assert '"name":"search"' in agent.tool_example("search")


def test_prompt_examples_use_tools_module_as_single_source(tmp_path, monkeypatch):
    monkeypatch.setitem(
        toolkit.TOOL_EXAMPLES,
        "search",
        '{"name":"search","arguments":{"pattern":"SINGLE_SOURCE","path":"."}}',
    )
    agent = build_agent(tmp_path, allowed_tools=["search"])

    prompt = agent.tool_example("search")

    assert "SINGLE_SOURCE" in prompt
