from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.context_manager import (
    ContextManager,
    DEFAULT_REDUCTION_ORDER,
    DEFAULT_SECTION_BUDGETS,
    DEFAULT_SECTION_FLOORS,
    DEFAULT_TOTAL_BUDGET,
    SECTION_ORDER,
)


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def test_context_manager_assembles_sections_in_expected_order(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record({"role": "user", "content": "old request", "created_at": "2026-04-07T09:59:00+00:00"})
    agent.record({"role": "assistant", "content": "old answer", "created_at": "2026-04-07T10:00:30+00:00"})

    prompt, metadata = ContextManager(agent).build("Where is the deploy key?")

    assert prompt.index("You are pico") < prompt.index("<workspace_state>")
    assert prompt.index("<workspace_state>") < prompt.index("Transcript:")
    assert prompt.index("Transcript:") < prompt.index("Current user request:")
    assert prompt.rstrip().endswith("Current user request:\nWhere is the deploy key?")
    assert "Working memory:" not in prompt
    assert "Relevant memory:" not in prompt
    assert metadata["section_order"] == ["prefix", "history", "current_request"]
    assert set(metadata["sections"]) == {"prefix", "history", "current_request"}
    assert "relevant_memory" not in metadata


def test_context_manager_default_prompt_budget_contract():
    assert SECTION_ORDER == ("prefix", "history", "current_request")
    assert DEFAULT_TOTAL_BUDGET == 15000
    assert DEFAULT_SECTION_BUDGETS == {"prefix": 7000, "history": 8000}
    assert DEFAULT_SECTION_FLOORS == {"prefix": 1200, "history": 1500}
    assert DEFAULT_REDUCTION_ORDER == ("history", "prefix")


def test_context_manager_uses_default_section_floors(tmp_path):
    agent = build_agent(tmp_path, [])

    assert ContextManager(agent).section_floors == DEFAULT_SECTION_FLOORS


def test_context_manager_reduces_history_before_prefix_and_preserves_newer_context(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    agent.record({"role": "user", "content": "OLD-CONTEXT " + ("D" * 260), "created_at": "2026-04-07T09:59:00+00:00"})
    for minute in range(1, 8):
        role = "assistant" if minute % 2 == 1 else "user"
        content = "RECENT-CONTEXT " + ("E" * 260) if minute == 7 else f"recent-{minute} " + ("E" * 180)
        agent.record({"role": role, "content": content, "created_at": f"2026-04-07T10:0{minute}:00+00:00"})

    manager = ContextManager(
        agent,
        total_budget=500,
        section_budgets={
            "prefix": 120,
            "history": 400,
        },
        section_floors={
            "prefix": 80,
            "history": 100,
        },
    )

    prompt, metadata = manager.build("keep this request verbatim")

    for section in ("prefix", "history"):
        assert metadata["sections"][section]["rendered_chars"] <= metadata["sections"][section]["budget_chars"]

    reduction_sections = [entry["section"] for entry in metadata["budget_reductions"]]
    assert reduction_sections[0] == "history"
    assert reduction_sections
    assert "RECENT-CONTEXT" in prompt
    assert "OLD-CONTEXT" not in prompt
    assert "keep this request verbatim" in prompt


def test_context_manager_reduces_prefix_after_history_reaches_floor(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 1000)
    for index in range(6):
        agent.record(
            {
                "role": "user",
                "content": f"history-{index} " + ("B" * 220),
                "created_at": f"2026-04-07T10:0{index}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(
        agent,
        total_budget=430,
        section_budgets={
            "prefix": 300,
            "history": 180,
        },
        section_floors={
            "prefix": 100,
            "history": 120,
        },
    ).build("recall")

    reduction_sections = [entry["section"] for entry in metadata["budget_reductions"]]
    assert reduction_sections[:2] == ["history", "prefix"]
    assert metadata["sections"]["history"]["budget_chars"] == 120
    assert metadata["sections"]["prefix"]["budget_chars"] < 300
    assert "recall" in prompt


def test_context_manager_reduction_stops_at_default_section_floors(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 10000)
    for index in range(20):
        agent.record(
            {
                "role": "user",
                "content": f"history-{index} " + ("B" * 900),
                "created_at": f"2026-04-07T10:{index:02d}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent, total_budget=1000).build("recall")

    reduction_sections = [entry["section"] for entry in metadata["budget_reductions"]]
    assert reduction_sections[:2] == ["history", "prefix"]
    assert metadata["sections"]["history"]["budget_chars"] == DEFAULT_SECTION_FLOORS["history"]
    assert metadata["sections"]["prefix"]["budget_chars"] == DEFAULT_SECTION_FLOORS["prefix"]
    assert "Current user request:\nrecall" in prompt


def test_context_manager_preserves_current_request_when_over_budget(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.prefix = "PREFIX " + ("A" * 600)
    for index in range(5):
        agent.record(
            {
                "role": "user",
                "content": f"history-{index} " + ("D" * 220),
                "created_at": f"2026-04-07T10:0{index}:00+00:00",
            }
        )

    request = "please preserve this request exactly"
    prompt, metadata = ContextManager(
        agent,
        total_budget=250,
        section_budgets={
            "prefix": 80,
            "history": 80,
        },
    ).build(request)

    assert prompt.split("Current user request:\n", 1)[1] == request
    assert metadata["current_request"]["text"] == request
    assert metadata["current_request"]["rendered_chars"] == len(request)


def test_context_manager_collapses_older_duplicate_reads_into_one_summary_line(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])
    agent.session["memory"]["file_summaries"]["sample.txt"] = {"summary": "alpha | beta"}

    for created_at in ("2026-04-07T09:00:00+00:00", "2026-04-07T09:01:00+00:00"):
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": "sample.txt", "start": 1, "end": 2},
                "content": "# sample.txt\nalpha\nbeta\n",
                "created_at": created_at,
            }
        )

    for minute in range(2, 8):
        role = "user" if minute % 2 == 0 else "assistant"
        agent.record(
            {
                "role": role,
                "content": f"recent-{minute}",
                "created_at": f"2026-04-07T09:0{minute}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent).build("check the file")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert transcript.count("[tool:read_file]") == 0
    assert "sample.txt -> alpha | beta" in transcript
    assert metadata["history"]["older_entries_count"] == 1
    assert metadata["history"]["collapsed_duplicate_reads"] == 1
    assert metadata["history"]["reused_file_summary_count"] == 1


def test_context_manager_summarizes_older_tool_output_into_one_line(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record(
        {
            "role": "tool",
            "name": "run_shell",
            "args": {"command": "pytest -q"},
            "content": "FAIL test_one\nFAIL test_two\nFAIL test_three\nFAIL test_four\n",
            "created_at": "2026-04-07T09:00:00+00:00",
        }
    )

    for minute in range(1, 7):
        role = "user" if minute % 2 == 1 else "assistant"
        agent.record(
            {
                "role": role,
                "content": f"recent-{minute}",
                "created_at": f"2026-04-07T09:0{minute}:00+00:00",
            }
        )

    prompt, metadata = ContextManager(agent).build("check failures")
    transcript = prompt.split("\n\nTranscript:\n", 1)[1].split("\n\nCurrent user request:", 1)[0]

    assert 'pytest -q -> FAIL test_one | FAIL test_two | FAIL test_three' in transcript
    assert "FAIL test_four" not in transcript
    assert metadata["history"]["summarized_tool_count"] == 1
    assert metadata["history"]["reused_file_summary_count"] == 0


def test_reusable_file_summary_reads_raw_session_file_summaries(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.session["memory"]["file_summaries"]["sample.txt"] = {"summary": "raw summary"}
    agent.session["memory"]["file_summaries"]["other.txt"] = "legacy string"
    manager = ContextManager(agent)

    assert manager._reusable_file_summary("sample.txt") == "raw summary"
    assert manager._reusable_file_summary("other.txt") == ""
    assert manager._reusable_file_summary("missing.txt") == ""


def test_prompt_cache_key_ignores_workspace_volatile_state(tmp_path):
    agent = build_agent(tmp_path, [])
    manager = ContextManager(agent)

    first_prompt, first = manager.build("first")
    agent.workspace.branch = "feature/cache-test"
    agent.workspace.status = " M README.md"
    agent.workspace.recent_commits = ["abc123 changed volatile state"]
    second_prompt, second = manager.build("second")

    assert first_prompt != second_prompt
    assert "feature/cache-test" in second_prompt
    assert first["base_prefix_hash"] == second["base_prefix_hash"]
    assert first["stable_prefix_hash"] == second["stable_prefix_hash"]
    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    assert first["prefix_hash"] == first["stable_prefix_hash"]


def test_prompt_cache_key_changes_when_memory_index_changes(tmp_path):
    memory_dir = tmp_path / ".pico" / "memory" / "notes"
    memory_dir.mkdir(parents=True)
    (memory_dir / "auth.md").write_text("# Auth notes\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])
    manager = ContextManager(agent)

    _, first = manager.build("first")
    (memory_dir / "deploy.md").write_text("# Deploy notes\n", encoding="utf-8")
    _, second = manager.build("second")

    assert first["prompt_cache_key"] != second["prompt_cache_key"]
    assert first["base_prefix_hash"] == second["base_prefix_hash"]


def test_runtime_prompt_metadata_uses_prefix_state_hash_for_base_prefix(tmp_path):
    agent = build_agent(tmp_path, [])

    prompt, metadata = agent._build_prompt_and_metadata("check cache metadata")

    assert metadata["base_prefix_hash"] == agent.prefix_state.hash
    assert metadata["stable_prefix_hash"] == metadata["prefix_hash"] == metadata["prompt_cache_key"]
    assert metadata["stable_prefix_hash"] != metadata["base_prefix_hash"]
    assert "memory_save" in prompt
    assert "<memory_index>" in prompt


def test_checkpoint_text_appears_in_history_after_workspace_state_not_prefix(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.render_checkpoint_text = lambda: "Task checkpoint:\nNext step: continue"

    prompt, metadata = ContextManager(agent).build("continue")

    prefix_text = prompt.split("\n\n<workspace_state>", 1)[0]
    assert "Task checkpoint:" not in prefix_text
    assert prompt.index("<workspace_state>") < prompt.index("Task checkpoint:")
    assert prompt.index("Task checkpoint:") < prompt.index("Transcript:")
    assert metadata["sections"]["history"]["raw_chars"] >= len("Task checkpoint:")


def test_history_budget_preserves_workspace_checkpoint_and_transcript_sentinels(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.workspace.status = "\n".join(
        f" M very/long/path_{index}.py " + ("x" * 120)
        for index in range(100)
    )
    agent.render_checkpoint_text = lambda: "Task checkpoint:\nNext step: continue"

    prompt, metadata = ContextManager(
        agent,
        total_budget=2200,
        section_budgets={
            "prefix": 500,
            "history": 500,
        },
    ).build("continue")

    assert "<workspace_state>" in prompt
    assert "Task checkpoint:" in prompt
    assert "Transcript:" in prompt
    assert prompt.index("<workspace_state>") < prompt.index("Task checkpoint:")
    assert prompt.index("Task checkpoint:") < prompt.index("Transcript:")
    assert prompt.split("Current user request:\n", 1)[1] == "continue"
    assert metadata["prompt_cache_key"] == metadata["stable_prefix_hash"]
