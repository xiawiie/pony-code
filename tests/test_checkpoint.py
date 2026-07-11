from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient
from pico.checkpoint import (
    CHECKPOINT_FULL_VALID_STATUS,
    CHECKPOINT_NONE_STATUS,
    create_checkpoint,
    current_runtime_identity,
    evaluate_resume_state,
)
from pico.task_state import TaskState


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy=kwargs.pop("approval_policy", "auto"),
        **kwargs,
    )


def test_current_runtime_identity_captures_execution_contract(tmp_path):
    agent = build_agent(tmp_path, max_steps=9, max_new_tokens=1024, read_only=True)

    identity = current_runtime_identity(agent)

    assert identity["session_id"] == agent.session["id"]
    assert identity["cwd"] == str(tmp_path)
    assert identity["read_only"] is True
    assert identity["max_steps"] == 9
    assert identity["max_new_tokens"] == 1024
    assert identity["workspace_fingerprint"] == agent.workspace.fingerprint()
    assert identity["tool_signature"] == agent.tool_signature()


def test_evaluate_resume_state_distinguishes_no_checkpoint_and_full_valid(tmp_path):
    agent = build_agent(tmp_path)

    assert evaluate_resume_state(agent)["status"] == CHECKPOINT_NONE_STATUS

    identity = current_runtime_identity(agent)
    agent.session["checkpoints"] = {
        "current_id": "ckpt_valid",
        "items": {
            "ckpt_valid": {
                "checkpoint_id": "ckpt_valid",
                "key_files": [],
                "runtime_identity": identity,
            }
        },
    }
    assert evaluate_resume_state(agent)["status"] == CHECKPOINT_FULL_VALID_STATUS

def test_create_checkpoint_records_recent_files_without_memory_state(tmp_path):
    agent = build_agent(tmp_path)
    (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")
    agent.memory.remember_file("sample.txt")
    agent._sync_working_memory()
    task_state = TaskState.create(task_id="task_test", user_request="read sample", run_id="run_test")
    task_state.finish_success("read sample")

    checkpoint = create_checkpoint(agent, task_state, "read sample", "unit")

    sample_item = next(item for item in checkpoint["key_files"] if item["path"] == "sample.txt")
    assert checkpoint["freshness"]["sample.txt"] == sample_item["freshness"]
    assert "memory_state" not in checkpoint
