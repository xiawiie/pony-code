import subprocess

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.tool_executor import ToolExecutor, ToolExecutionResult


def build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def init_git_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "pico@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Pico Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)


def test_tool_executor_returns_content_and_metadata_without_side_channel(tmp_path):
    agent = build_agent(tmp_path)

    result = ToolExecutor(agent).execute("read_file", {"path": "README.md", "start": 1, "end": 1})

    assert isinstance(result, ToolExecutionResult)
    assert "# README.md" in result.content
    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["read_only"] is True
    assert result.metadata["workspace_changed"] is False


def test_pico_run_tool_keeps_compatibility_metadata(tmp_path):
    agent = build_agent(tmp_path)

    content = agent.run_tool("read_file", {"path": "README.md", "start": 1, "end": 1})

    assert "# README.md" in content
    assert agent._last_tool_result_metadata["tool_status"] == "ok"


def test_write_file_creates_finalized_tool_change(tmp_path):
    agent = build_agent(tmp_path)

    result = agent.execute_tool("write_file", {"path": "note.txt", "content": "hello\n"})

    tool_change = agent.checkpoint_store.load_tool_change_record(result.metadata["tool_change_id"])
    assert tool_change["status"] == "finalized"
    assert tool_change["affected_paths"] == ["note.txt"]
    assert tool_change["file_entries"][0]["change_kind"] == "created"


def test_invalid_tool_args_do_not_create_pending_tool_change(tmp_path):
    agent = build_agent(tmp_path)

    result = agent.execute_tool("patch_file", {"path": "README.md", "old_text": "missing", "new_text": "x"})

    assert result.metadata["tool_status"] == "rejected"
    assert "tool_change_id" not in result.metadata or result.metadata["tool_change_id"] == ""


def test_tool_runtime_exception_finalizes_pending_record_as_error(tmp_path):
    agent = build_agent(tmp_path)

    def boom(args):
        raise RuntimeError("boom")

    agent.tools["write_file"]["run"] = boom
    result = agent.execute_tool("write_file", {"path": "note.txt", "content": "hello\n"})

    assert result.metadata["tool_status"] == "error"
    tool_change = agent.checkpoint_store.load_tool_change_record(result.metadata["tool_change_id"])
    assert tool_change["status"] == "error"
    assert tool_change["error"]["code"] == "tool_failed"


def test_success_and_exception_paths_use_shared_side_effect_finalizer(tmp_path, monkeypatch):
    import pico.tool_executor as tool_executor

    calls = []
    original = tool_executor._finalize_tool_side_effects

    def spy_finalize(*args, **kwargs):
        calls.append(kwargs["tool_status"])
        return original(*args, **kwargs)

    monkeypatch.setattr(tool_executor, "_finalize_tool_side_effects", spy_finalize)

    agent = build_agent(tmp_path)
    success = agent.execute_tool("write_file", {"path": "ok.txt", "content": "ok\n"})

    def boom(args):
        (tmp_path / args["path"]).write_text("partial\n", encoding="utf-8")
        raise RuntimeError("boom")

    agent.tools["write_file"]["run"] = boom
    failure = agent.execute_tool("write_file", {"path": "partial.txt", "content": "unused\n"})

    assert success.metadata["tool_status"] == "ok"
    assert failure.metadata["tool_status"] == "partial_success"
    assert calls == ["ok", "partial_success"]


def test_run_shell_uses_command_policy_metadata(tmp_path):
    agent = build_agent(tmp_path)

    result = agent.execute_tool("run_shell", {"command": "printf hello > generated.txt", "timeout": 5})

    assert result.metadata["command_risk_class"] == "workspace_write"
    assert result.metadata["command_approval"]["decision"] == "allow"
    assert "generated.txt" in result.metadata["affected_paths"]


def test_destructive_run_shell_is_not_auto_approved(tmp_path):
    agent = build_agent(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")

    result = agent.execute_tool("run_shell", {"command": "rm -f victim.txt", "timeout": 5})

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "command_approval_required"
    assert result.metadata["command_risk_class"] == "destructive"
    assert victim.exists()
    assert "tool_change_id" not in result.metadata


def test_destructive_run_shell_wrapped_forms_are_not_auto_approved(tmp_path):
    for command in (
        "rm -f victim.txt $(echo ignored)",
        "(rm -f victim.txt)",
        "{ rm -f victim.txt; }",
        "echo ok\nrm -f victim.txt",
        "env rm -f victim.txt",
        "find . -name victim.txt -delete",
        "git -C . reset --hard",
    ):
        agent = build_agent(tmp_path)
        victim = tmp_path / "victim.txt"
        victim.write_text("keep\n", encoding="utf-8")

        result = agent.execute_tool("run_shell", {"command": command, "timeout": 5})

        assert result.metadata["tool_status"] == "rejected"
        assert result.metadata["tool_error_code"] == "command_approval_required"
        assert result.metadata["command_risk_class"] == "destructive"
        assert victim.read_text(encoding="utf-8") == "keep\n"
        assert "tool_change_id" not in result.metadata


def test_write_file_recovery_does_not_use_full_workspace_snapshot(tmp_path):
    agent = build_agent(tmp_path)

    def fail_full_snapshot():
        raise AssertionError("full snapshot should not run")

    agent.capture_workspace_snapshot = fail_full_snapshot

    result = agent.execute_tool("write_file", {"path": "note.txt", "content": "hello\n"})

    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["affected_paths"] == ["note.txt"]


def test_run_shell_recovery_does_not_use_full_workspace_snapshot(tmp_path):
    agent = build_agent(tmp_path)

    def fail_full_snapshot():
        raise AssertionError("full snapshot should not run")

    agent.capture_workspace_snapshot = fail_full_snapshot

    result = agent.execute_tool("run_shell", {"command": "printf hello > generated.txt", "timeout": 5})

    assert result.metadata["tool_status"] == "ok"
    assert "generated.txt" in result.metadata["affected_paths"]


def test_run_shell_recovery_does_not_blob_unrelated_dirty_paths(tmp_path):
    agent = build_agent(tmp_path)
    init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("user dirty\n", encoding="utf-8")
    written_blobs = []
    original_write_blob = agent.checkpoint_store.write_blob

    def spy_write_blob(data, content_kind="text"):
        written_blobs.append(bytes(data))
        return original_write_blob(data, content_kind)

    agent.checkpoint_store.write_blob = spy_write_blob

    result = agent.execute_tool("run_shell", {"command": "printf hello > generated.txt", "timeout": 5})

    assert result.metadata["tool_status"] == "ok"
    assert "generated.txt" in result.metadata["affected_paths"]
    assert b"user dirty\n" not in written_blobs


def test_run_shell_recovery_marks_dirty_before_tracked_file_unrestorable(tmp_path):
    agent = build_agent(tmp_path)
    init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("user dirty\n", encoding="utf-8")

    result = agent.execute_tool("run_shell", {"command": "printf 'tool changed\\n' > README.md", "timeout": 5})

    assert result.metadata["tool_status"] == "ok"
    entry = result.metadata["file_entries"][0]
    assert entry["path"] == "README.md"
    assert entry["change_kind"] == "modified"
    assert entry["before_blob_ref"] == ""
    assert entry["snapshot_eligible"] is False
    assert entry["ineligible_reason"] == "before_blob_unavailable"


def test_run_shell_recovery_populates_before_blob_from_git_head(tmp_path):
    # clean tracked file: observer 看不到，HEAD fallback 应该把 before-blob 抓下来。
    agent = build_agent(tmp_path)
    init_git_repo(tmp_path)

    result = agent.execute_tool("run_shell", {"command": "printf 'tool changed\\n' > README.md", "timeout": 5})

    assert result.metadata["tool_status"] == "ok"
    entry = result.metadata["file_entries"][0]
    assert entry["path"] == "README.md"
    assert entry["change_kind"] == "modified"
    assert entry["before_blob_ref"]
    assert agent.checkpoint_store.read_blob(entry["before_blob_ref"]) == b"demo\n"
    assert entry["snapshot_eligible"] is True
    assert result.metadata["diff_summary"] == ["modified:README.md"]


def test_workspace_write_tool_uses_generic_path_argument_for_recovery(tmp_path):
    agent = build_agent(tmp_path)

    def custom_write(args):
        (tmp_path / args["path"]).write_text("custom\n", encoding="utf-8")
        return "ok"

    agent.tools["custom_write"] = {
        "schema": {"path": "str"},
        "risky": True,
        "description": "Custom write tool used by the test.",
        "run": custom_write,
    }

    result = agent.execute_tool("custom_write", {"path": "custom.txt"})

    assert result.metadata["tool_status"] == "ok"
    assert result.metadata["affected_paths"] == ["custom.txt"]
    assert result.metadata["file_entries"][0]["path"] == "custom.txt"


def test_generic_path_arg_registry_covers_destination_and_paths_list(tmp_path):
    # 目的：future tool（move_file / delete_files 等）用非 "path" 参数时也要能记录 recovery。
    agent = build_agent(tmp_path)
    (tmp_path / "existing.txt").write_text("original\n", encoding="utf-8")

    def custom_move(args):
        source = tmp_path / args["source"]
        destination = tmp_path / args["destination"]
        destination.write_bytes(source.read_bytes())
        source.unlink()
        return "moved"

    agent.tools["custom_move"] = {
        "schema": {"source": "str", "destination": "str"},
        "risky": True,
        "description": "Move a file.",
        "run": custom_move,
    }

    result = agent.execute_tool("custom_move", {"source": "existing.txt", "destination": "moved.txt"})

    assert result.metadata["tool_status"] == "ok"
    affected = set(result.metadata["affected_paths"])
    assert "existing.txt" in affected
    assert "moved.txt" in affected


def test_generic_path_arg_registry_covers_list_arg(tmp_path):
    agent = build_agent(tmp_path)
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")

    def custom_delete(args):
        for path in args["paths"]:
            (tmp_path / path).unlink()
        return "deleted"

    agent.tools["custom_delete"] = {
        "schema": {"paths": "list[str]"},
        "risky": True,
        "description": "Delete files.",
        "run": custom_delete,
    }

    result = agent.execute_tool("custom_delete", {"paths": ["a.txt", "b.txt"]})

    assert result.metadata["tool_status"] == "ok"
    affected = set(result.metadata["affected_paths"])
    assert affected == {"a.txt", "b.txt"}


def test_recovery_lifecycle_uses_effect_class_not_risky_flag(tmp_path):
    agent = build_agent(tmp_path)
    agent.model_client.outputs.append("<final>delegated</final>")

    result = agent.execute_tool("delegate", {"task": "inspect README", "max_steps": 1})

    assert result.metadata["tool_change_id"]
    tool_change = agent.checkpoint_store.load_tool_change_record(result.metadata["tool_change_id"])
    assert tool_change["tool_name"] == "delegate"
    assert tool_change["effect_class"] == "workspace_write"
