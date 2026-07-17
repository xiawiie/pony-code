import importlib.util
import json
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


@pytest.mark.parametrize("version", [None, True, 1.0, "1", 2])
def test_load_scenarios_validates_header_before_filter(tmp_path, version):
    module = _load_memory_benchmark_module()
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    payload = {
        "record_type": "memory_quality_scenario",
        "format_version": version,
        "id": "not-selected",
        "session_turns": [],
    }
    if version is None:
        payload.pop("format_version")
    (scenario_dir / "scenario_bad.jsonl").write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(module.ScenarioLoadError, match="format_version"):
        list(
            module.load_scenarios(
                filter_id="selected",
                scenario_dir=scenario_dir,
            )
        )


def test_load_scenarios_rejects_wrong_type_and_nested_duplicate_keys(tmp_path):
    module = _load_memory_benchmark_module()
    scenario_dir = tmp_path / "scenarios"
    scenario_dir.mkdir()
    path = scenario_dir / "scenario_bad.jsonl"
    path.write_text(
        '{"record_type":"memory_quality_scenario","format_version":1,'
        '"id":"bad","session_turns":[],"nested":{"key":1,"key":2}}\n',
        encoding="utf-8",
    )
    with pytest.raises(module.ScenarioLoadError, match="duplicate"):
        list(module.load_scenarios(scenario_dir=scenario_dir))

    path.write_text(
        json.dumps(
            {
                "record_type": "memory_quality_result",
                "format_version": 1,
                "id": "bad",
                "session_turns": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(module.ScenarioLoadError, match="record_type"):
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


def test_setup_workspace_maps_workspace_notes_to_pony_memory(tmp_path):
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
        ws / ".pony" / "memory" / "notes" / "auth.md"
    ).read_text(encoding="utf-8") == "# Auth\n\n- bcrypt rounds must be <= 12\n"
    assert (
        ws / ".pony" / "memory" / "agent_notes.md"
    ).read_text(encoding="utf-8") == "- old lesson\n"


def test_setup_workspace_maps_user_scope_notes(tmp_path):
    module = _load_memory_benchmark_module()

    ws = module.setup_workspace(
        {
            "id": "user_scope",
            "setup_notes": {"user/notes/prefs.md": "Prefer grouped reviews.\n"},
            "session_turns": [],
        },
        parent_dir=tmp_path,
    )

    assert (
        ws / ".pony" / "benchmark-user-memory" / "notes" / "prefs.md"
    ).read_text(encoding="utf-8") == "Prefer grouped reviews.\n"


def test_parse_memory_search_hits_extracts_paths_and_scores():
    module = _load_memory_benchmark_module()

    hits = module.parse_memory_search_hits(
        "Found 2 match(es) for 'bcrypt':\n"
        "- workspace/notes/auth.md (score=1.23)\n"
        "  L3: bcrypt rounds\n"
        "- workspace/notes/session.md (score=0.75)\n"
    )

    assert hits == [
        {"path": "workspace/notes/auth.md", "score": 1.23},
        {"path": "workspace/notes/session.md", "score": 0.75},
    ]

    logical = module.parse_memory_search_hits(
        "- workspace/agent_notes.md#entry-4 (score=2.50)\n"
    )
    assert logical == [
        {"path": "workspace/agent_notes.md#entry-4", "score": 2.5}
    ]


def test_score_recall_requires_expected_hit_in_top_three(tmp_path):
    module = _load_memory_benchmark_module()
    scenario = {
        "id": "recall_bcrypt",
        "setup_notes": {},
        "session_turns": [
            {
                "user": "how should login hashing work?",
                "expected_search_hit": "workspace/notes/auth.md",
            }
        ],
    }
    trace_events = [
        {
            "event": "tool_executed",
            "name": "memory_search",
            "args": {"query": "hashing", "limit": 5},
            "result": "- workspace/notes/auth.md (score=1.23)\n",
            "tool_status": "ok",
        }
    ]

    row = module.score_scenario(scenario, trace_events, tmp_path)

    assert row["status"] == "pass"
    assert row["tool_calls"] == ["memory_search"]
    assert row["expected_hits"] == ["workspace/notes/auth.md"]
    assert row["observed_hits"] == ["workspace/notes/auth.md"]
    assert row["failure_reason"] == ""


def test_score_update_requires_memory_save_and_preserves_existing_note(tmp_path):
    module = _load_memory_benchmark_module()
    memory_root = tmp_path / ".pony" / "memory"
    memory_root.mkdir(parents=True)
    (memory_root / "agent_notes.md").write_text(
        "- old lesson\n- bcrypt rounds > 12 causes CI timeout\n",
        encoding="utf-8",
    )
    scenario = {
        "id": "update_bcrypt_lesson",
        "setup_notes": {"workspace/agent_notes.md": "- old lesson\n"},
        "session_turns": [
            {
                "user": "please remember: bcrypt rounds > 12 causes CI timeout",
                "expected_tool": "memory_save",
            }
        ],
    }
    trace_events = [
        {
            "event": "tool_executed",
            "name": "memory_save",
            "args": {"note": "bcrypt rounds > 12 causes CI timeout"},
            "result": "saved: workspace/agent_notes.md (chars_total=80)",
            "tool_status": "ok",
        }
    ]

    row = module.score_scenario(scenario, trace_events, tmp_path)

    assert row["status"] == "pass"
    assert row["tool_calls"] == ["memory_save"]
    assert row["agent_notes_changed"] is True
    assert row["failure_reason"] == ""


def test_score_no_noise_passes_without_memory_search(tmp_path):
    module = _load_memory_benchmark_module()
    scenario = {
        "id": "no_noise_food",
        "setup_notes": {},
        "session_turns": [
            {
                "user": "what is for lunch?",
                "expected_no_search_hit": True,
            }
        ],
    }

    row = module.score_scenario(scenario, [], tmp_path)

    assert row["status"] == "pass"
    assert row["tool_calls"] == []
    assert row["observed_hits"] == []


def test_score_no_noise_fails_on_high_scoring_irrelevant_hit(tmp_path):
    module = _load_memory_benchmark_module()
    scenario = {
        "id": "no_noise_food",
        "setup_notes": {},
        "session_turns": [
            {
                "user": "what is for lunch?",
                "expected_no_search_hit": True,
            }
        ],
    }
    trace_events = [
        {
            "event": "tool_executed",
            "name": "memory_search",
            "args": {"query": "lunch"},
            "result": "- workspace/notes/auth.md (score=1.25)\n",
            "tool_status": "ok",
        }
    ]

    row = module.score_scenario(scenario, trace_events, tmp_path)

    assert row["status"] == "fail"
    assert row["failure_reason"] == "unexpected high-scoring memory hit"


def test_score_recall_fails_when_expected_hit_is_fourth_in_search_result(tmp_path):
    module = _load_memory_benchmark_module()
    scenario = {
        "id": "recall_bcrypt",
        "setup_notes": {},
        "session_turns": [
            {
                "user": "how should login hashing work?",
                "expected_search_hit": "workspace/notes/auth.md",
            }
        ],
    }
    trace_events = [
        {
            "event": "tool_executed",
            "name": "memory_search",
            "args": {"query": "hashing", "limit": 5},
            "result": (
                "- workspace/notes/one.md (score=1.50)\n"
                "- workspace/notes/two.md (score=1.40)\n"
                "- workspace/notes/three.md (score=1.30)\n"
                "- workspace/notes/auth.md (score=1.20)\n"
            ),
            "tool_status": "ok",
        }
    ]

    row = module.score_scenario(scenario, trace_events, tmp_path)

    assert row["status"] == "fail"
    assert row["failure_reason"] == "missing expected memory hit: workspace/notes/auth.md"


def test_score_recall_passes_when_expected_hit_is_top_three_in_later_search(tmp_path):
    module = _load_memory_benchmark_module()
    scenario = {
        "id": "recall_bcrypt",
        "setup_notes": {},
        "session_turns": [
            {
                "user": "how should login hashing work?",
                "expected_search_hit": "workspace/notes/auth.md",
            }
        ],
    }
    trace_events = [
        {
            "event": "tool_executed",
            "name": "memory_search",
            "args": {"query": "login"},
            "result": (
                "- workspace/notes/one.md (score=1.50)\n"
                "- workspace/notes/two.md (score=1.40)\n"
                "- workspace/notes/three.md (score=1.30)\n"
            ),
            "tool_status": "ok",
        },
        {
            "event": "tool_executed",
            "name": "memory_search",
            "args": {"query": "hashing"},
            "result": "- workspace/notes/auth.md (score=1.20)\n",
            "tool_status": "ok",
        },
    ]

    row = module.score_scenario(scenario, trace_events, tmp_path)

    assert row["status"] == "pass"
    assert row["failure_reason"] == ""


def test_score_expected_hits_top_requires_all_hits_in_one_search_event(tmp_path):
    module = _load_memory_benchmark_module()
    scenario = {
        "id": "recall_auth_and_session",
        "setup_notes": {},
        "session_turns": [
            {
                "user": "what should auth sessions remember?",
                "expected_search_hits_top": [
                    "workspace/notes/auth.md",
                    "workspace/notes/session.md",
                ],
            }
        ],
    }

    split_row = module.score_scenario(
        scenario,
        [
            {
                "event": "tool_executed",
                "name": "memory_search",
                "args": {"query": "auth"},
                "result": "- workspace/notes/auth.md (score=1.30)\n",
                "tool_status": "ok",
            },
            {
                "event": "tool_executed",
                "name": "memory_search",
                "args": {"query": "session"},
                "result": "- workspace/notes/session.md (score=1.20)\n",
                "tool_status": "ok",
            },
        ],
        tmp_path,
    )

    assert split_row["status"] == "fail"
    assert (
        split_row["failure_reason"]
        == "missing expected memory hit: workspace/notes/auth.md, workspace/notes/session.md"
    )

    same_event_row = module.score_scenario(
        scenario,
        [
            {
                "event": "tool_executed",
                "name": "memory_search",
                "args": {"query": "auth session"},
                "result": (
                    "- workspace/notes/auth.md (score=1.30)\n"
                    "- workspace/notes/session.md (score=1.20)\n"
                ),
                "tool_status": "ok",
            }
        ],
        tmp_path,
    )

    assert same_event_row["status"] == "pass"
    assert same_event_row["failure_reason"] == ""


def test_score_memory_save_requires_successful_tool_status(tmp_path):
    module = _load_memory_benchmark_module()
    memory_root = tmp_path / ".pony" / "memory"
    memory_root.mkdir(parents=True)
    (memory_root / "agent_notes.md").write_text(
        "- old lesson\n- bcrypt rounds > 12 causes CI timeout\n",
        encoding="utf-8",
    )
    scenario = {
        "id": "update_bcrypt_lesson",
        "setup_notes": {"workspace/agent_notes.md": "- old lesson\n"},
        "session_turns": [
            {
                "user": "please remember: bcrypt rounds > 12 causes CI timeout",
                "expected_tool": "memory_save",
            }
        ],
    }
    trace_events = [
        {
            "event": "tool_executed",
            "name": "memory_save",
            "args": {"note": "bcrypt rounds > 12 causes CI timeout"},
            "result": "rejected",
            "tool_status": "rejected",
        }
    ]

    row = module.score_scenario(scenario, trace_events, tmp_path)

    assert row["status"] == "fail"
    assert row["failure_reason"] == "successful memory_save was not called"


def test_score_memory_save_requires_expected_note_in_args(tmp_path):
    module = _load_memory_benchmark_module()
    memory_root = tmp_path / ".pony" / "memory"
    memory_root.mkdir(parents=True)
    (memory_root / "agent_notes.md").write_text(
        "- old lesson\n- bcrypt rounds > 12 causes CI timeout\n",
        encoding="utf-8",
    )
    scenario = {
        "id": "update_bcrypt_lesson",
        "setup_notes": {"workspace/agent_notes.md": "- old lesson\n"},
        "session_turns": [
            {
                "user": "please remember: bcrypt rounds > 12 causes CI timeout",
                "expected_tool": "memory_save",
            }
        ],
    }
    trace_events = [
        {
            "event": "tool_executed",
            "name": "memory_save",
            "args": {"note": "unrelated note"},
            "result": "saved: workspace/agent_notes.md (chars_total=80)",
            "tool_status": "ok",
        }
    ]

    row = module.score_scenario(scenario, trace_events, tmp_path)

    assert row["status"] == "fail"
    assert row["failure_reason"] == "memory_save note args did not include expected note"


def test_fake_mode_outputs_json_summary_without_human_text(capsys):
    module = _load_memory_benchmark_module()

    code = module.main(["--mode", "fake", "--format", "json", "--scenario", "recall_bcrypt"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert captured.out.lstrip().startswith("{")
    assert "=== summary ===" not in captured.out
    assert payload["record_type"] == "memory_quality_result"
    assert payload["format_version"] == 1
    assert payload["mode"] == "fake"
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["failed"] == 0
    assert payload["rows"][0]["id"] == "recall_bcrypt"
    assert payload["rows"][0]["status"] == "pass"
    assert "memory_search" in payload["rows"][0]["tool_calls"]


@pytest.mark.parametrize("version", [None, True, 1.0, "1", 2])
def test_memory_result_render_rejects_noncurrent_header_before_business(version):
    module = _load_memory_benchmark_module()
    payload = {
        "record_type": "memory_quality_result",
        "format_version": version,
        "summary": "poisoned-business-shape",
        "rows": "poisoned-business-shape",
    }
    if version is None:
        payload.pop("format_version")

    with pytest.raises(ValueError, match="format_version"):
        module.render_text(payload)


def test_memory_result_rejects_wrong_type_and_nested_duplicate_file(tmp_path):
    module = _load_memory_benchmark_module()
    with pytest.raises(ValueError, match="record_type"):
        module.render_text(
            {
                "record_type": "memory_quality_scenario",
                "format_version": 1,
                "summary": "poisoned-business-shape",
                "rows": "poisoned-business-shape",
            }
        )

    path = tmp_path / "result.json"
    path.write_text(
        '{"record_type":"memory_quality_result","format_version":1,'
        '"summary":{"total":1,"total":2},"rows":[]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        module.load_result(path)


def test_fake_mode_scores_update_scenario(capsys):
    module = _load_memory_benchmark_module()

    code = module.main(["--mode", "fake", "--format", "json", "--scenario", "update_bcrypt_lesson"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    row = payload["rows"][0]
    assert row["status"] == "pass"
    assert row["tool_calls"] == ["memory_save"]
    assert row["agent_notes_changed"] is True


def test_fake_mode_scores_no_noise_without_memory_search(capsys):
    module = _load_memory_benchmark_module()

    code = module.main(["--mode", "fake", "--format", "json", "--scenario", "no_noise_food"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    row = payload["rows"][0]
    assert row["status"] == "pass"
    assert row["tool_calls"] == ["memory_search"]
    assert row["observed_hits"] == []


def test_fake_mode_full_benchmark_outputs_zero_failures(capsys):
    module = _load_memory_benchmark_module()

    code = module.main(["--mode", "fake", "--format", "json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["summary"]["total"] == 33
    assert payload["summary"]["failed"] == 0
    assert payload["summary"]["by_category"]["conflicting_fact"]["total"] == 2
    assert payload["summary"]["by_category"]["false_recall"]["total"] == 3
    assert payload["summary"]["by_category"]["stale_fact"]["total"] == 2


def test_semantic_benchmark_has_required_quality_categories():
    module = _load_memory_benchmark_module()
    scenarios = [scenario for _stem, scenario in module.load_scenarios()]

    assert len(scenarios) >= 30
    assert {
        "chinese",
        "paraphrase",
        "conflicting_fact",
        "stale_fact",
        "long_notes",
        "prompt_injection",
        "false_recall",
        "deletion",
        "cross_scope",
        "multi_hop",
    } <= {scenario.get("category") for scenario in scenarios}


def test_fake_mode_ignores_user_memory_root(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    user_notes = home / ".pony" / "memory" / "notes"
    user_notes.mkdir(parents=True)
    noisy_note = (
        "auth session testing bcrypt hash passwords login endpoint async tests "
        "pytest asyncio_mode auto cookies SameSite flow "
        "密码 加密 异步 测试 配置 认证 会话 "
    )
    for index in range(4):
        (user_notes / f"noisy_{index}.md").write_text(noisy_note * 20, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    module = _load_memory_benchmark_module()

    code = module.main(["--mode", "fake", "--format", "json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["summary"]["failed"] == 0
    for row in payload["rows"]:
        assert not any(
            path.startswith("user/notes/noisy_")
            for path in row["observed_hits"]
        )
