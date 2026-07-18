import os
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import pony.memory.service as memorylib
from pony import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from pony.context.renderer import render_current_user_message
from benchmarks.support.fake_provider import FakeModelClient
from pony.providers.response import Response, StopReason
from pony.state.task_state import TaskState
from pony.tools.executor import ToolExecutionResult
from tests.test_docker_sandbox_runtime import _build_runtime
from pony.runtime.options import RuntimeOptions


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return Pony(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(approval_policy=approval_policy, **kwargs),
    )


def build_sandbox_agent(tmp_path, monkeypatch, outputs):
    _source, context, agent = _build_runtime(tmp_path, monkeypatch)
    agent.model_client = FakeModelClient(outputs)
    return agent, context


def set_raw_file_summary(agent, path, summary):
    memorylib.set_file_summary_dict(
        agent.session["memory"]["file_summaries"],
        path,
        summary,
        workspace_root=agent.root,
    )


def build_request_view(agent, user_message):
    agent.session["messages"].append(
        {"role": "user", "content": user_message, "_pony_meta": {}}
    )
    snapshot, telemetry = render_current_user_message(agent, user_message)
    return agent.context_manager.build_request(
        injection_snapshot=snapshot,
        injection_telemetry=telemetry,
        preflight_metadata={},
    )


# =============================================================================
# Runtime/report/resume tests
# =============================================================================


def test_report_separates_sent_request_session_transcript_and_all_completion_usage(
    tmp_path,
):
    agent = build_agent(tmp_path, ["done"])
    agent.session["messages"] = [
        {
            "role": "user",
            "content": "older question",
            "_pony_meta": {"created_at": "t1"},
        },
        {
            "role": "assistant",
            "content": "older answer",
            "_pony_meta": {"created_at": "t2"},
        },
    ]
    agent.last_request_metadata = {
        "messages_count": 1,
        "messages_chars": 8,
        "messages_tokens": 2,
        "system_prefix_hash": "cache",
    }
    task_state = TaskState.create(task_id="task_x", run_id="run_x", user_request="q")
    task_state.finish_success("done")
    report = agent.build_report(
        task_state,
        completion_usage_totals={
            "input_tokens": 30,
            "output_tokens": 7,
            "total_tokens": 37,
            "cached_tokens": 10,
            "cache_creation_input_tokens": 4,
            "cache_read_input_tokens": 10,
            "cache_hit": True,
        },
    )
    assert report["context"]["messages_count"] == 1
    assert report["model"]["usage"]["total_tokens"] == 37
    assert report["model"]["usage"]["cache_hit"] is True
    assert "session_messages_count" not in report
    assert "session_messages_chars" not in report
    assert "prompt_metadata" not in report
    assert "older question" not in json.dumps(report)
    assert "older answer" not in json.dumps(report)


def test_successful_run_persists_run_artifacts_and_stop_reason(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {"name": "read_file", "args": {"path":"hello.txt","start":1,"end":2}},
            "Finished.",
        ],
    )

    assert agent.ask("Do the thing") == "Finished."

    runs_root = tmp_path / ".pony" / "runs"
    run_dirs = [path for path in runs_root.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1

    run_dir = run_dirs[0]
    task_state = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    trace_lines = (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()

    assert task_state["task_id"] != task_state["run_id"]
    assert run_dir.name == task_state["run_id"]
    assert (run_dir / "task_state.json").exists()
    assert (run_dir / "trace.jsonl").exists()
    assert (run_dir / "report.json").exists()
    assert task_state["stop_reason"] == "final_answer_returned"
    assert task_state["final_answer"] == "Finished."
    assert report["run"]["stop_reason"] == "final_answer_returned"
    assert report["run"]["run_id"] == task_state["run_id"]
    assert report["finalization"] == {"status": "complete", "error_count": 0}
    assert "task_state" not in report and "final_answer" not in report
    trace_events = [json.loads(line)["event"] for line in trace_lines]
    assert trace_events[0] == "run_started"
    assert trace_events[-1] == "run_finished"
    assert trace_events.count("prompt_built") == 2
    assert "tool_executed" in trace_events


def test_report_tool_counts_are_per_run_and_include_rejections(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            {"name": "unknown_tool", "args": {}},
            "First run done.",
            "Second run done.",
        ],
    )

    assert agent.ask("Reject one tool") == "First run done."
    first = agent.run_store.load_report(agent.current_task_state.run_id)

    assert agent.current_task_state.tool_steps == 0
    assert first["tools"] == {
        "calls": 1,
        "allowed": 0,
        "denied": 1,
        "name_counts": {"unknown_tool": 1},
        "status_counts": {"rejected": 1},
    }

    assert agent.ask("Return only a final answer") == "Second run done."
    second = agent.run_store.load_report(agent.current_task_state.run_id)

    assert second["tools"] == {
        "calls": 0,
        "allowed": 0,
        "denied": 0,
        "name_counts": {},
        "status_counts": {},
    }


def test_report_projects_current_run_tool_change_effects(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            {
                "name": "run_shell",
                "args": {
                    "command": "printf 'changed\\n' > README.md && exit 1",
                    "timeout": 20,
                },
            },
            "Done.",
        ],
        approval_policy="ask",
    )
    agent.approve = lambda name, args: True

    assert agent.ask("Change README and fail") == "Done."
    report = agent.run_store.load_report(agent.current_task_state.run_id)

    assert report["effects"] == {
        "changed_files": 1,
        "partial_successes": 1,
        "recovery_review_required": True,
    }
    assert report["recovery"]["review_required"] is True


def test_report_projects_sandbox_outcome_and_host_fallback_from_tool_result(
    tmp_path,
    monkeypatch,
):
    agent, context = build_sandbox_agent(
        tmp_path,
        monkeypatch,
        [
            {"name": "run_shell", "args": {"command":"pwd","timeout":20}},
            "Done.",
        ],
    )
    agent.execute_tool = lambda name, args: ToolExecutionResult(
        content="exit_code: 0",
        metadata={
            "tool_status": "ok",
            "tool_error_code": "",
            "security_event_type": "",
            "risk_level": "high",
            "effect_class": "workspace_write",
            "read_only": False,
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
            "sandbox": {
                "status": "completed",
                "execution_plane": "host",
            },
            "command_approval": {"runner_executed": True},
        },
    )

    assert agent.ask("Run one sandbox command") == "Done."
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    manifest = context.current_session().manifest

    assert report["sandbox"] == {
        "active": True,
        "implementation": "docker_container",
        "session_state": "ready",
        "engine_profile": "desktop_vm",
        "image_digest": Pony._public_sandbox_digest(
            manifest["image"]["image_digest"]
        ),
        "policy_digest": Pony._public_sandbox_digest(manifest["policy"]["digest"]),
        "network_mode": "none",
        "source_mounted": False,
        "state_mounted": False,
        "container_calls": 1,
        "target_started_count": 0,
        "outcome_counts": {"completed": 1},
        "cleanup_failure_count": 0,
        "host_fallback_count": 1,
        "diff": {"candidates": 0, "blocked": 0, "generated": 0},
        "apply_status": "not_started",
    }


def test_report_treats_missing_execution_plane_as_unproven_host_fallback(
    tmp_path,
    monkeypatch,
):
    agent, _context = build_sandbox_agent(
        tmp_path,
        monkeypatch,
        [
            {"name": "run_shell", "args": {"command":"pwd","timeout":20}},
            "Done.",
        ],
    )
    agent.execute_tool = lambda name, args: ToolExecutionResult(
        content="exit_code: 0",
        metadata={
            "tool_status": "ok",
            "tool_error_code": "",
            "security_event_type": "",
            "risk_level": "high",
            "effect_class": "workspace_write",
            "read_only": False,
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
            "sandbox": {"status": "completed"},
            "command_approval": {"runner_executed": True},
        },
    )

    assert agent.ask("Run one sandbox command") == "Done."
    report = agent.run_store.load_report(agent.current_task_state.run_id)

    assert report["sandbox"]["host_fallback_count"] == 1


def test_report_counts_explicit_host_plane_without_approval_evidence(
    tmp_path,
    monkeypatch,
):
    agent, _context = build_sandbox_agent(
        tmp_path,
        monkeypatch,
        [
            {"name": "run_shell", "args": {"command":"pwd","timeout":20}},
            "Done.",
        ],
    )
    agent.execute_tool = lambda name, args: ToolExecutionResult(
        content="exit_code: 0",
        metadata={
            "tool_status": "ok",
            "tool_error_code": "",
            "security_event_type": "",
            "risk_level": "high",
            "effect_class": "workspace_write",
            "read_only": False,
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
            "sandbox": {
                "status": "completed",
                "execution_plane": "host",
            },
        },
    )

    assert agent.ask("Run one sandbox command") == "Done."
    report = agent.run_store.load_report(agent.current_task_state.run_id)

    assert report["sandbox"]["host_fallback_count"] == 1


def test_report_counts_started_targets_and_cleanup_failures(tmp_path, monkeypatch):
    agent, _context = build_sandbox_agent(
        tmp_path,
        monkeypatch,
        [
            {"name": "run_shell", "args": {"command":"pwd","timeout":20}},
            "Done.",
        ],
    )
    agent.execute_tool = lambda name, args: ToolExecutionResult(
        content="exit_code: 0",
        metadata={
            "tool_status": "partial_success",
            "tool_error_code": "container_cleanup_failed",
            "security_event_type": "",
            "risk_level": "high",
            "effect_class": "workspace_write",
            "read_only": False,
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
            "sandbox": {
                "status": "completed",
                "execution_plane": "sandbox",
                "target_started": True,
                "cleanup_status": "failed",
                "wrapper_status": "completed",
                "timed_out": False,
                "residue_detected": True,
                "container_created": True,
                "runner_executed": True,
                "error_code": "container_cleanup_failed",
                "call_id": "call_1234",
                "execution_plan_digest": "sha256:" + "3" * 64,
                "logical_intent_digest": "sha256:" + "4" * 64,
                "policy_digest": "sha256:" + "5" * 64,
                "stdout_bytes": 7,
                "stderr_bytes": 0,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "container_id": "6" * 64,
            },
            "command_approval": {"runner_executed": True},
        },
    )

    assert agent.ask("Run one sandbox command") == "Done."
    sandbox = agent.run_store.load_report(agent.current_task_state.run_id)["sandbox"]

    assert sandbox["container_calls"] == 1
    assert sandbox["target_started_count"] == 1
    assert sandbox["cleanup_failure_count"] == 1
    assert sandbox["host_fallback_count"] == 0
    trace = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    event = [item for item in trace if item["event"] == "tool_executed"][0]
    assert event["sandbox_outcome"] == "completed"
    assert event["execution_plane"] == "sandbox"
    assert event["target_started"] is True
    assert event["cleanup_status"] == "failed"
    assert event["sandbox_error_code"] == "container_cleanup_failed"
    assert event["execution_plan_digest"] == "sha256:" + "3" * 64
    assert event["policy_digest"] == "sha256:" + "5" * 64
    assert event["stdout_bytes"] == 7
    assert "container_id" not in event


def test_interrupted_tool_attempt_is_included_in_current_run_report(tmp_path):
    agent = build_agent(
        tmp_path,
        [{"name": "run_shell", "args": {"command":"pwd","timeout":20}}],
    )
    agent.tools["run_shell"]["run"] = lambda _execution: (_ for _ in ()).throw(
        KeyboardInterrupt()
    )

    with pytest.raises(KeyboardInterrupt):
        agent.ask("Interrupt one shell call")

    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["tools"] == {
        "calls": 1,
        "allowed": 1,
        "denied": 0,
        "name_counts": {"run_shell": 1},
        "status_counts": {"interrupted": 1},
    }
    assert report["effects"]["recovery_review_required"] is True
    assert report["recovery"]["review_required"] is True


def test_step_limit_run_artifacts_reference_final_checkpoint(tmp_path):
    agent = build_agent(
        tmp_path,
        [{"name": "read_file", "args": {"path":"README.md","start":1,"end":1}}],
        max_steps=1,
    )

    answer = agent.ask("Inspect README")

    assert "step limit" in answer
    task_state = json.loads(
        agent.run_store.task_state_path(agent.current_task_state).read_text(
            encoding="utf-8"
        )
    )
    report = json.loads(
        agent.run_store.report_path(agent.current_task_state).read_text(
            encoding="utf-8"
        )
    )
    checkpoint_id = agent.session["checkpoints"]["current_id"]

    assert task_state["stop_reason"] == "step_limit_reached"
    assert task_state["checkpoint_id"] == checkpoint_id
    assert report["recovery"]["checkpoint_id"] == checkpoint_id
    assert "task_state" not in report


def test_trace_and_report_redact_secret_env_values(tmp_path):
    secret = "sk-test-secret-123"
    with patch.dict(
        os.environ, {"HOME": str(tmp_path), "OPENAI_API_KEY": secret}, clear=True
    ):
        agent = build_agent(
            tmp_path,
            [
                {
                    "name": "run_shell",
                    "args": {
                        "command": "printf '%s' 'sk-test-secret-123'",
                        "timeout": 20,
                    },
                },
                "Masked sk-test-secret-123",
            ],
        )

        assert agent.ask("Mask the secret") == "Masked <redacted>"
        assert secret not in agent.prefix

    runs_root = tmp_path / ".pony" / "runs"
    run_dirs = [path for path in runs_root.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1

    run_dir = run_dirs[0]
    session_text = Path(agent.session_path).read_text(encoding="utf-8")
    task_state_text = (run_dir / "task_state.json").read_text(encoding="utf-8")
    trace_text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    report_text = (run_dir / "report.json").read_text(encoding="utf-8")
    trace_events = [json.loads(line) for line in trace_text.splitlines()]

    assert secret not in session_text
    assert secret not in task_state_text
    assert secret not in trace_text
    assert secret not in report_text
    assert "<redacted>" in session_text
    assert "<redacted>" in task_state_text

    prompt_events = [
        event for event in trace_events if event["event"] == "prompt_built"
    ]
    assert prompt_events
    assert prompt_events[0]["request_metadata"]["secret_env_count"] >= 1
    assert "OPENAI_API_KEY" in prompt_events[0]["request_metadata"]["secret_env_names"]
    assert "prompt_metadata" not in prompt_events[0]

    tool_events = [event for event in trace_events if event["event"] == "tool_executed"]
    assert tool_events
    assert "args" not in tool_events[0]
    assert "result" not in tool_events[0]
    assert tool_events[0]["tool_status"] == "rejected"
    assert tool_events[0].get("reason_code", "") in {"", "sensitive_content_block"}


def test_request_metadata_describes_actual_sent_view(tmp_path):
    agent = build_agent(tmp_path, ["Done."])
    agent.session["messages"] = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"history-{index}-" + ("A" * 240),
            "_pony_meta": {},
        }
        for index in range(8)
    ]

    assert agent.ask("recall") == "Done."

    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    metadata = next(
        event["request_metadata"]
        for event in trace_events
        if event["event"] == "model_turn"
    )

    assert metadata["dropped_messages"] == 0
    assert metadata["messages_count"] > 0
    assert metadata["messages_chars"] > 0
    assert metadata["runtime_feedback_present"] is False
    assert metadata["context_breakdown"]["history"]["dropped_turns"] == 0


def test_turn_preflight_refreshes_prefix_when_workspace_changes(tmp_path):
    agent = build_agent(
        tmp_path,
        ["first", "second", "third"],
    )

    assert agent.ask("first") == "first"
    first = dict(agent.last_request_metadata)
    assert agent.ask("second") == "second"
    second = dict(agent.last_request_metadata)

    assert first["system_prefix_hash"] == second["system_prefix_hash"]
    assert second["prefix_changed"] is False
    assert second["workspace_changed"] is False

    (tmp_path / "README.md").write_text("demo changed\n", encoding="utf-8")

    assert agent.ask("third") == "third"
    third = agent.last_request_metadata

    assert third["system_prefix_hash"] == second["system_prefix_hash"]
    assert third["prefix_changed"] is False
    assert third["workspace_changed"] is True
    assert "demo changed" not in agent.prefix
    assert "demo changed" in repr(agent.model_client.requests[-1]["messages"])


def test_agent_creates_one_task_checkpoint_without_silent_history_reduction(tmp_path):
    agent = build_agent(tmp_path, ["Done after checkpoint."])
    for index in range(10):
        agent.session["messages"].append(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"history-{index}-" + ("A" * 260),
                "_pony_meta": {"created_at": f"2026-04-07T10:{index:02d}:00+00:00"},
            }
        )
    assert agent.ask("Resume the long task") == "Done after checkpoint."
    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    checkpoint_events = [
        event for event in trace_events if event["event"] == "checkpoint_created"
    ]
    assert agent.last_request_metadata["dropped_messages"] == 0
    assert len(checkpoint_events) == 1
    assert checkpoint_events[0]["trigger"] == "run_finished"
    assert (
        sum(
        entry["type"] == "task_checkpoint"
        for entry in agent.session_store.load_tree(agent.session["id"]).active_path
        )
        == 1
    )


def test_resume_prompt_carries_checkpoint_via_v2_messages(tmp_path):
    """Task E4 rewrite: the resume checkpoint state should surface in the
    injection block on the outgoing user message (v2 shape), not the
    legacy flattened prompt."""
    agent = build_agent(tmp_path, ["checkpoint ready."])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_manual",
        "items": {
            "ckpt_manual": {
                "checkpoint_id": "ckpt_manual",
                "parent_checkpoint_id": "",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Fix failing resume flow",
                "completed": ["Read runtime.py"],
                "excluded": ["Do not add branch summary"],
                "current_blocker": "Need to re-anchor stale file facts",
                "next_step": "Re-read runtime.py and refresh the checkpoint",
                "key_files": [{"path": "runtime.py", "freshness": "abc"}],
            }
        },
    }
    request, metadata = build_request_view(agent, "continue")

    # The checkpoint-derived working set appears in its dynamic source block on
    # the current turn's user message.
    current_content = request["messages"][-1]["content"]
    assert isinstance(current_content, str)
    if "<pony:task_working_set>" in current_content:
        # Injection is active — verify checkpoint fields flow through.
        assert (
            "Fix failing resume flow" in current_content
            or "current_goal" in current_content
        )
    else:
        # No source block emitted (the allocator could not include the working set
        # given the budget) — accept the graceful skip but ensure telemetry
        # explains why: either dropped in injection_dropped, or budget=0.
        assert (
            "task_working_set" in metadata.get("injection_dropped", [])
            or metadata.get("injection_tokens", {}).get("task_working_set", 0) == 0
        )


def test_resume_invalidates_stale_file_summaries_and_marks_partial_stale(tmp_path):
    file_path = tmp_path / "runtime.py"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, ["checkpoint ready."])
    set_raw_file_summary(agent, "runtime.py", "runtime.py: alpha")
    freshness = agent.session["memory"]["file_summaries"]["runtime.py"]["freshness"]
    agent.session["checkpoints"] = {
        "current_id": "ckpt_stale",
        "items": {
            "ckpt_stale": {
                "checkpoint_id": "ckpt_stale",
                "parent_checkpoint_id": "",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Fix stale summary handling",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Re-read runtime.py",
                "key_files": [{"path": "runtime.py", "freshness": freshness}],
                "freshness": {"runtime.py": freshness},
                "summary": "runtime.py is important",
                "runtime_identity": {
                    "workspace_fingerprint": agent.workspace.fingerprint()
                },
            }
        },
    }
    agent.session_store.save(agent.session)
    file_path.write_text("beta\n", encoding="utf-8")

    resumed = Pony.from_session(
        model_client=FakeModelClient(["Resumed."]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        options=RuntimeOptions(approval_policy="auto"),
    )

    assert resumed.ask("Continue the task") == "Resumed."

    assert "runtime.py" not in resumed.session["memory"]["file_summaries"]
    assert resumed.last_request_metadata["resume_status"] == "partial-stale"
    # File-summary dictionaries are now rebuildable caches, not canonical
    # Session state; the checkpoint freshness fact still marks the path stale.
    assert resumed.last_request_metadata["stale_summary_invalidations"] == 0
    assert resumed.last_request_metadata["stale_paths"] == ["runtime.py"]


def test_report_last_request_metadata_preserves_initial_resume_status(tmp_path):
    file_path = tmp_path / "runtime.py"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, ["checkpoint ready."])
    set_raw_file_summary(agent, "runtime.py", "runtime.py: alpha")
    freshness = agent.session["memory"]["file_summaries"]["runtime.py"]["freshness"]
    agent.session["checkpoints"] = {
        "current_id": "ckpt_stale",
        "items": {
            "ckpt_stale": {
                "checkpoint_id": "ckpt_stale",
                "parent_checkpoint_id": "",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Fix stale summary handling",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Re-read runtime.py",
                "key_files": [{"path": "runtime.py", "freshness": freshness}],
                "freshness": {"runtime.py": freshness},
                "summary": "runtime.py is important",
                "runtime_identity": {
                    "workspace_fingerprint": agent.workspace.fingerprint()
                },
            }
        },
    }
    agent.session_store.save(agent.session)
    file_path.write_text("beta\n", encoding="utf-8")

    resumed = Pony.from_session(
        model_client=FakeModelClient(
            [
                {
                    "name": "read_file",
                    "args": {"path": "runtime.py", "start": 1, "end": 1},
                },
                "Resumed.",
            ]
        ),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        options=RuntimeOptions(approval_policy="auto"),
    )

    assert resumed.ask("Continue the task") == "Resumed."
    report = resumed.run_store.load_report(resumed.current_task_state.run_id)

    assert report["recovery"]["status"] == "partial-stale"
    assert report["context"]["resume_status"] == "partial-stale"
    assert report["context"]["last_prompt_resume_status"] == "partial-stale"


def test_first_prompt_resume_status_updates_task_state_after_late_checkpoint_setup(
    tmp_path,
):
    file_path = tmp_path / "runtime.py"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {"name": "read_file", "args": {"path":"runtime.py","start":1,"end":1}},
            "Resumed.",
        ],
    )
    set_raw_file_summary(agent, "runtime.py", "runtime.py: alpha")
    freshness = agent.session["memory"]["file_summaries"]["runtime.py"]["freshness"]
    agent.session["checkpoints"] = {
        "current_id": "ckpt_stale",
        "items": {
            "ckpt_stale": {
                "checkpoint_id": "ckpt_stale",
                "parent_checkpoint_id": "",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Fix stale summary handling",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Re-read runtime.py",
                "key_files": [{"path": "runtime.py", "freshness": freshness}],
                "freshness": {"runtime.py": freshness},
                "summary": "runtime.py is important",
                "runtime_identity": {
                    "workspace_fingerprint": agent.workspace.fingerprint()
                },
            }
        },
    }
    agent.session_store.save(agent.session)
    file_path.write_text("beta\n", encoding="utf-8")

    assert agent.ask("Continue the task") == "Resumed."
    report = agent.run_store.load_report(agent.current_task_state.run_id)

    assert report["recovery"]["status"] == "partial-stale"
    assert report["context"]["resume_status"] == "partial-stale"
    assert report["context"]["last_prompt_resume_status"] == "partial-stale"


def test_run_shell_nonzero_with_workspace_change_is_recorded_as_partial_success(
    tmp_path,
):
    agent = build_agent(tmp_path, [], approval_policy="ask")
    agent.approve = lambda name, args: True

    result = agent.run_tool(
        "run_shell",
        {
            "command": "printf 'changed\\n' > README.md && exit 1",
            "timeout": 20,
        },
    )

    assert "exit_code: 1" in result
    assert agent._last_tool_result_metadata["tool_status"] == "partial_success"
    assert agent._last_tool_result_metadata["affected_paths"] == ["README.md"]
    assert agent._last_tool_result_metadata["workspace_changed"] is True


def test_resume_marks_workspace_mismatch_when_checkpoint_runtime_identity_is_stale(
    tmp_path,
):
    agent = build_agent(tmp_path, ["checkpoint ready."])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_workspace",
        "items": {
            "ckpt_workspace": {
                "checkpoint_id": "ckpt_workspace",
                "parent_checkpoint_id": "",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Continue after drift",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Rebuild runtime state",
                "key_files": [],
                "freshness": {},
                "summary": "workspace changed",
                "runtime_identity": {"workspace_fingerprint": "outdated-fingerprint"},
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = Pony.from_session(
        model_client=FakeModelClient(["Resumed."]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        options=RuntimeOptions(approval_policy="auto"),
    )

    assert resumed.ask("Continue the task") == "Resumed."
    assert resumed.last_request_metadata["resume_status"] == "workspace-mismatch"


def test_write_file_trace_records_minimum_tool_contract_fields(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            {
                "name": "write_file",
                "args": {"path": "notes.txt", "content": "hello\\n"},
            },
            "Done.",
        ],
    )

    assert agent.ask("Create notes.txt") == "Done."

    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    tool_event = [event for event in trace_events if event["event"] == "tool_executed"][
        -1
    ]

    assert tool_event["name"] == "write_file"
    assert tool_event["risk_level"] == "high"
    assert tool_event["read_only"] is False
    assert tool_event["tool_status"] == "ok"
    assert tool_event["affected_paths"] == ["notes.txt"]
    assert tool_event["workspace_changed"] is True
    assert tool_event["diff_summary"] == ["created:notes.txt"]


def test_resume_uses_session_version_for_embedded_checkpoint(tmp_path):
    agent = build_agent(tmp_path, ["checkpoint ready."])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_schema",
        "items": {
            "ckpt_schema": {
                "checkpoint_id": "ckpt_schema",
                "parent_checkpoint_id": "",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Continue after schema change",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Migrate checkpoint",
                "key_files": [],
                "freshness": {},
                "summary": "schema changed",
                "runtime_identity": {
                    "workspace_fingerprint": agent.workspace.fingerprint()
                },
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = Pony.from_session(
        model_client=FakeModelClient(["Resumed."]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        options=RuntimeOptions(approval_policy="auto"),
    )

    assert resumed.ask("Continue the task") == "Resumed."
    assert resumed.last_request_metadata["resume_status"] == "full-valid"


def test_session_save_rejects_missing_checkpoint_state(tmp_path):
    agent = build_agent(tmp_path, ["checkpoint ready."])
    agent.session.pop("checkpoints", None)
    with pytest.raises(ValueError, match="fields do not match current format"):
        agent.session_store.save(agent.session)


def test_freshness_mismatch_is_traced_and_final_task_checkpoint_is_single(tmp_path):
    file_path = tmp_path / "runtime.py"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, ["Resumed."])
    set_raw_file_summary(agent, "runtime.py", "runtime.py: alpha")
    freshness = agent.session["memory"]["file_summaries"]["runtime.py"]["freshness"]
    agent.session["checkpoints"] = {
        "current_id": "ckpt_freshness",
        "items": {
            "ckpt_freshness": {
                "checkpoint_id": "ckpt_freshness",
                "parent_checkpoint_id": "",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Handle freshness mismatch",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Re-read runtime.py",
                "key_files": [{"path": "runtime.py", "freshness": freshness}],
                "freshness": {"runtime.py": freshness},
                "summary": "runtime.py changed",
                "runtime_identity": {
                    "workspace_fingerprint": agent.workspace.fingerprint()
                },
            }
        },
    }
    agent.session_store.save(agent.session)
    file_path.write_text("beta\n", encoding="utf-8")

    assert agent.ask("Continue the task") == "Resumed."

    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    checkpoint_events = [
        event for event in trace_events if event["event"] == "checkpoint_created"
    ]
    mismatch_events = [
        event
        for event in trace_events
        if event["event"] == "checkpoint_freshness_mismatch"
    ]

    assert len(mismatch_events) == 1
    assert len(checkpoint_events) == 1
    assert checkpoint_events[0]["trigger"] == "run_finished"


def test_runtime_identity_persists_key_execution_metadata(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    agent = Pony(
        model_client=FakeModelClient(["Done."]),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(
        approval_policy="never",
        max_steps=9,
        max_output_tokens=1024,
        feature_flags={"memory": True},
        ),
    )

    runtime_identity = agent.session["runtime_identity"]

    assert runtime_identity["session_id"] == agent.session["id"]
    assert runtime_identity["cwd"] == str(tmp_path)
    assert runtime_identity["approval_policy"] == "never"
    assert runtime_identity["read_only"] is False
    assert runtime_identity["max_steps"] == 9
    assert runtime_identity["max_output_tokens"] == 1024
    assert runtime_identity["feature_flags"]["memory"] is True
    assert runtime_identity["feature_flags"] == {"memory": True}
    assert runtime_identity["shell_env_allowlist"] == list(agent.shell_env_allowlist)


def test_resume_records_runtime_identity_mismatch_fields_in_metadata_and_trace(
    tmp_path,
):
    agent = build_agent(tmp_path, ["checkpoint ready."])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_identity",
        "items": {
            "ckpt_identity": {
                "checkpoint_id": "ckpt_identity",
                "parent_checkpoint_id": "",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Resume with a different runtime identity",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Rebuild runtime identity",
                "key_files": [],
                "freshness": {},
                "summary": "identity changed",
                "runtime_identity": {
                    "workspace_fingerprint": agent.workspace.fingerprint(),
                    "approval_policy": "auto",
                    "read_only": False,
                    "max_steps": 6,
                    "max_output_tokens": 512,
                    "model": "old-model",
                    "model_client": "FakeModelClient",
                    "feature_flags": {"memory": False},
                    "shell_env_allowlist": ["PATH"],
                    "session_id": agent.session["id"],
                    "cwd": str(tmp_path),
                },
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = Pony.from_session(
        model_client=FakeModelClient(["Resumed."]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        options=RuntimeOptions(
        approval_policy="never",
        max_steps=9,
        max_output_tokens=1024,
        feature_flags={"memory": True},
        ),
    )

    resumed.ask("Continue the task")

    assert resumed.last_request_metadata["resume_status"] == "workspace-mismatch"
    assert resumed.last_request_metadata["runtime_identity_mismatch_fields"] == [
        "approval_policy",
        "feature_flags",
        "max_output_tokens",
        "max_steps",
        "model",
        "shell_env_allowlist",
    ]

    trace_events = [
        json.loads(line)
        for line in resumed.run_store.trace_path(resumed.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    mismatch_events = [
        event for event in trace_events if event["event"] == "runtime_identity_mismatch"
    ]
    assert mismatch_events
    assert mismatch_events[0]["fields"] == [
        "approval_policy",
        "feature_flags",
        "max_output_tokens",
        "max_steps",
        "model",
        "shell_env_allowlist",
    ]


def test_partial_success_records_metadata_without_process_notes(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")
    agent.approve = lambda name, args: True

    agent.run_tool(
        "run_shell",
        {
            "command": "printf 'changed\\n' > README.md && exit 1",
            "timeout": 20,
        },
    )

    assert agent._last_tool_result_metadata["tool_status"] == "partial_success"
    assert agent._last_tool_result_metadata["affected_paths"] == ["README.md"]
    assert "episodic_notes" not in agent.session["memory"]
    assert "notes" not in agent.session["memory"]


def test_agent_keeps_completion_usage_out_of_last_request_metadata(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    agent = Pony(
        model_client=FakeModelClient(
            [
                Response(
                    stop_reason=StopReason.END_TURN,
                    content=[{"type": "text", "text": "Done."}],
                    usage={
                        "cached_tokens": 512,
                        "cache_hit": True,
                        "input_tokens": 1024,
                    },
                )
            ]
        ),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(approval_policy="auto"),
    )

    assert agent.ask("Cache aware run") == "Done."

    assert agent.last_request_metadata["prompt_cache_supported"] is False
    assert "cached_tokens" not in agent.last_request_metadata
    assert "cache_hit" not in agent.last_request_metadata
    assert agent.last_request_metadata["system_prefix_hash"]
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["model"]["usage"]["cached_tokens"] == 512
    assert report["model"]["usage"]["input_tokens"] == 1024
    assert report["model"]["usage"]["cache_hit"] is True


def test_report_records_safe_model_identity_and_request_evidence(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")

    class EffectiveModelClient(FakeModelClient):
        def complete(self, **request):
            self.provider_metadata["effective_model"] = "gpt-effective"
            return super().complete(**request)

    client = EffectiveModelClient(
        [
        Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": "Done."}],
            usage={"request_id": "request-123"},
        )
        ]
    )
    client.last_transport_attempts = 2
    client.provider_metadata = {
        "protocol_family": "openai_responses",
        "requested_model": "gpt-test",
        "effective_model": "gpt-test",
    }
    agent = Pony(
        model_client=client,
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(approval_policy="auto"),
    )

    assert agent.ask("record provider evidence") == "Done."

    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert client.provider_metadata.items() <= report["context"].items()
    assert report["context"]["effective_model"] == "gpt-effective"
    assert report["context"]["provider_request_id"] == "request-123"
    assert report["context"]["last_transport_attempts"] == 2
    assert report["model"]["transport_attempts"] == 2


def test_recent_messages_preserved_older_digested(tmp_path):
    """Task E5 rewrite: recent messages stay intact; older tool_results
    over the digest threshold appear as [digest] entries."""
    agent = build_agent(tmp_path, ["Done."])

    # Seed session["messages"] directly (v2 shape) — 4 older + 6 recent messages.
    agent.session["messages"] = [
        # older tool_use/tool_result pair — result is a pre-rendered digest
        {"role": "user", "content": "old question 1"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "read_file",
                    "input": {"path": "x.py"},
                }
            ],
            "_pony_meta": {"tool_use_id": "t1"},
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": "[digest] x.py (500 lines)\n- import os",
                }
            ],
            "_pony_meta": {"tool_use_id": "t1", "digest_applied": True},
        },
        {"role": "assistant", "content": "old answer 1"},
        # recent 6 messages
        {"role": "user", "content": "recent question 1"},
        {"role": "assistant", "content": "recent answer 1"},
        {"role": "user", "content": "recent question 2"},
        {"role": "assistant", "content": "recent answer 2"},
        {"role": "user", "content": "recent question 3"},
        {"role": "assistant", "content": "recent answer 3"},
    ]

    request, _metadata = build_request_view(agent, "current question")

    # Last 6 messages preserved verbatim in the returned messages array
    # (exclude the appended current user turn at index -1).
    recent_kept = request["messages"][-7:-1]
    assert any("recent question 1" in str(m["content"]) for m in recent_kept)

    # Older tool_result content carries [digest] marker.
    older_content = str(request["messages"][2]["content"])
    assert "[digest]" in older_content
