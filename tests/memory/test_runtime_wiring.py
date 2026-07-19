"""验证 Pony 运行时把 BlockStore / Retrieval / RepoMap wire 到 ToolContext。"""

import json
from pathlib import Path

from pony import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from benchmarks.support.fake_provider import FakeModelClient
import pony.memory.service as memorylib
from pony.state.session_store import SESSION_FORMAT_VERSION, SESSION_RECORD_TYPE
from pony.state.task_state import TaskState
from pony.runtime.options import RuntimeOptions


def _isolate_home(monkeypatch, tmp_path):
    """让 Path.home() 指向 tmp_path/home, 隔离用户本机 ~/.pony/."""
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    return fake_home


def test_pony_has_memory_store_and_repo_map(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    (tmp_path / "AGENTS.md").write_text("# project\n")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    agent = Pony(
        model_client=FakeModelClient(["done"]),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True),
    )
    assert agent.memory_store is not None
    assert agent.memory_retrieval is not None
    assert agent.repo_map is not None


def test_tool_context_has_wiring(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    agent = Pony(
        model_client=FakeModelClient(["done"]),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True),
    )
    ctx = agent.tool_context()
    assert ctx.memory_store is agent.memory_store
    assert ctx.memory_retrieval is agent.memory_retrieval
    assert ctx.repo_map is agent.repo_map


def _build_agent(tmp_path, monkeypatch, session=None):
    _isolate_home(monkeypatch, tmp_path)
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    return Pony(
        model_client=FakeModelClient(["done"]),
        workspace=workspace,
        session_store=store,
        session=session,
        options=RuntimeOptions(
            project_trusted=True,
            delegate_model_client_factory=lambda: FakeModelClient(["child done"]),
        ),
    )


def test_runtime_uses_current_working_memory(tmp_path, monkeypatch):
    sample = tmp_path / "sample.txt"
    sample.write_text("current\n", encoding="utf-8")
    current_session = {
        "record_type": SESSION_RECORD_TYPE,
        "format_version": SESSION_FORMAT_VERSION,
        "id": "current",
        "created_at": "2026-04-07T10:00:00+00:00",
        "workspace_root": str(tmp_path),
        "messages": [],
        "working_memory": {
            "task_summary": "Current task",
            "recent_files": [sample],
        },
        "memory": {
            "file_summaries": {
                sample: {
                    "summary": "current summary",
                    "created_at": "2026-04-07T10:00:00+00:00",
                    "freshness": memorylib.file_freshness(sample, tmp_path),
                }
            },
        },
        "recently_recalled": [],
        "checkpoints": {"current_id": "", "items": {}},
        "resume_state": {},
        "runtime_identity": {},
        "permission_mode": "auto",
    }

    agent = _build_agent(tmp_path, monkeypatch, session=current_session)

    assert type(agent.memory).__name__ == "WorkingMemory"
    assert agent.session["working_memory"] == {
        "task_summary": "Current task",
        "recent_files": ["sample.txt"],
    }
    assert set(agent.session["memory"]) == {"file_summaries"}
    assert (
        agent.session["memory"]["file_summaries"]["sample.txt"]["summary"]
        == "current summary"
    )


def test_read_file_updates_working_memory_and_raw_file_summary(tmp_path, monkeypatch):
    (tmp_path / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = _build_agent(tmp_path, monkeypatch)

    agent.update_memory_after_tool("read_file", {"path": "sample.txt"}, "alpha\nbeta\n")

    assert agent.session["working_memory"]["recent_files"] == ["sample.txt"]
    assert (
        agent.session["memory"]["file_summaries"]["sample.txt"]["summary"]
        == "alpha | beta"
    )


def test_write_file_invalidates_raw_summary_and_keeps_recent_files_synced(
    tmp_path, monkeypatch
):
    (tmp_path / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = _build_agent(tmp_path, monkeypatch)
    agent.update_memory_after_tool("read_file", {"path": "sample.txt"}, "alpha\nbeta\n")

    agent.update_memory_after_tool("write_file", {"path": "sample.txt"}, "ok")

    assert agent.session["working_memory"]["recent_files"] == ["sample.txt"]
    assert "sample.txt" not in agent.session["memory"]["file_summaries"]


def test_patch_file_invalidates_raw_summary_and_keeps_recent_files_synced(
    tmp_path, monkeypatch
):
    (tmp_path / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = _build_agent(tmp_path, monkeypatch)
    agent.update_memory_after_tool("read_file", {"path": "sample.txt"}, "alpha\nbeta\n")

    agent.update_memory_after_tool("patch_file", {"path": "sample.txt"}, "ok")

    assert agent.session["working_memory"]["recent_files"] == ["sample.txt"]
    assert "sample.txt" not in agent.session["memory"]["file_summaries"]


def test_invalidate_stale_memory_removes_changed_raw_summary(tmp_path, monkeypatch):
    sample = tmp_path / "sample.txt"
    sample.write_text("alpha\nbeta\n", encoding="utf-8")
    agent = _build_agent(tmp_path, monkeypatch)
    agent.update_memory_after_tool("read_file", {"path": "sample.txt"}, "alpha\nbeta\n")

    sample.write_text("changed\n", encoding="utf-8")
    invalidated = agent.invalidate_stale_memory()

    assert invalidated == ["sample.txt"]
    assert agent.session["memory"] == {"file_summaries": {}}


def test_reset_clears_messages_working_memory_and_keeps_narrow_memory_shape(
    tmp_path, monkeypatch
):
    (tmp_path / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = _build_agent(tmp_path, monkeypatch)
    agent.session["messages"].append(
        {
        "role": "user",
        "content": "hello",
        "_pony_meta": {"created_at": "t"},
        }
    )
    agent.memory.set_task_summary("Task")
    agent._sync_working_memory()
    agent.update_memory_after_tool("read_file", {"path": "sample.txt"}, "alpha\nbeta\n")

    agent.reset()

    assert agent.session["messages"] == []
    assert type(agent.memory).__name__ == "WorkingMemory"
    assert agent.session["working_memory"] == {"task_summary": "", "recent_files": []}
    assert agent.session["memory"] == {"file_summaries": {}}


def test_memory_text_returns_working_memory_json(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    agent.memory.set_task_summary("Inspect runtime")
    agent.memory.remember_file("pony/runtime/application.py")
    agent._sync_working_memory()

    assert json.loads(agent.memory_text()) == {
        "task_summary": "Inspect runtime",
        "recent_files": ["pony/runtime/application.py"],
    }


def test_build_report_excludes_working_memory_content(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    agent.memory.set_task_summary("Report task")
    agent.memory.remember_file("pony/runtime/application.py")
    agent._sync_working_memory()
    task_state = TaskState.create(
        run_id="run_test", task_id="task_test", user_request="Report task"
    )
    task_state.finish_success("done")

    report = agent.build_report(task_state)

    assert "working_memory" not in report
    assert "Report task" not in json.dumps(report)
    assert "pony/runtime/application.py" not in json.dumps(report)


def test_normal_ask_final_answer_creates_checkpoint_and_syncs_working_memory(
    tmp_path, monkeypatch
):
    agent = _build_agent(tmp_path, monkeypatch)

    assert agent.ask("Final only") == "done"

    assert agent.session["working_memory"]["task_summary"] == "Final only"
    assert agent.session["checkpoints"]["current_id"]


def test_spawn_delegate_syncs_child_working_memory_without_notes(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    captured = {}

    def fake_ask(child, user_message):
        captured["user_message"] = user_message
        captured["working_memory"] = dict(child.session["working_memory"])
        captured["memory"] = dict(child.session["memory"])
        return "child done"

    monkeypatch.setattr(Pony, "ask", fake_ask)

    assert (
        agent.spawn_delegate({"task": "Inspect child state", "max_steps": 1})
        == "delegate_result:\nchild done"
    )
    assert captured["user_message"] == "Inspect child state"
    assert captured["working_memory"] == {
        "task_summary": "Inspect child state",
        "recent_files": [],
    }
    assert captured["memory"] == {"file_summaries": {}}


def test_process_note_hook_is_removed_from_runtime_and_tool_executor():
    hook_name = "record_process" + "_note_for_tool"
    runtime_source = Path("pony/runtime/application.py").read_text(encoding="utf-8")
    tool_executor_source = Path("pony/tools/executor.py").read_text(encoding="utf-8")

    assert hook_name not in runtime_source
    assert hook_name not in tool_executor_source
