import importlib.util
from pathlib import Path

import pytest


def _load_memory_benchmark_module():
    spec = importlib.util.spec_from_file_location(
        "memory_quality_benchmark",
        Path("benchmarks/memory_quality/run_benchmark.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_scenarios_reports_json_file_and_line(tmp_path):
    module = _load_memory_benchmark_module()
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    (scenario_dir / "scenario_bad.jsonl").write_text('{"id": "bad"\n', encoding="utf-8")

    with pytest.raises(module.ScenarioLoadError, match=r"scenario_bad\.jsonl:1"):
        list(module.load_scenarios(scenario_dir=scenario_dir))


def test_setup_workspace_rejects_invalid_setup_path(tmp_path):
    module = _load_memory_benchmark_module()

    with pytest.raises(ValueError, match="invalid setup note path"):
        module.setup_workspace(
            {
                "id": "bad_path",
                "setup_notes": {"../outside.md": "secret"},
                "session_turns": [],
            },
            parent_dir=tmp_path,
        )

    assert not any(tmp_path.iterdir())


def test_setup_workspace_rejects_non_dict_setup_notes_without_side_effects(tmp_path):
    module = _load_memory_benchmark_module()

    with pytest.raises(ValueError, match="setup_notes must be an object"):
        module.setup_workspace(
            {
                "id": "bad_setup_notes",
                "setup_notes": ["workspace/notes/auth.md"],
                "session_turns": [],
            },
            parent_dir=tmp_path,
        )

    assert not any(tmp_path.iterdir())


def test_setup_workspace_maps_workspace_notes_to_pico_memory(tmp_path):
    module = _load_memory_benchmark_module()

    ws = module.setup_workspace(
        {
            "id": "recall_bcrypt",
            "setup_notes": {
                "workspace/notes/auth.md": "# Auth\n\n- bcrypt rounds must be <= 12\n",
                "workspace/agent_notes.md": "- old lesson\n",
            },
            "session_turns": [],
        },
        parent_dir=tmp_path,
    )

    assert (ws / "AGENTS.md").read_text(encoding="utf-8") == "# Test project\n"
    assert (
        ws / ".pico" / "memory" / "notes" / "auth.md"
    ).read_text(encoding="utf-8") == "# Auth\n\n- bcrypt rounds must be <= 12\n"
    assert (
        ws / ".pico" / "memory" / "agent_notes.md"
    ).read_text(encoding="utf-8") == "- old lesson\n"
