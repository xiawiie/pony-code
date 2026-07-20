import json

from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.runtime.options import RuntimeOptions
from pony.state.session_store import SessionStore
from pony.state.task_state import TaskState
from pony.workspace.context import WorkspaceContext


def _agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )


def test_trace_listener_receives_a_copy_after_durable_append(tmp_path):
    agent = _agent(tmp_path)
    received = []

    def listener(event):
        received.append(event)
        event["event"] = "mutated"

    agent._trace_listener = listener
    state = TaskState.create(run_id="run", task_id="task", user_request="test")
    agent.run_store.start_run(state)

    returned = agent.emit_trace(state, "run_started", {})

    assert received[0]["event"] == "mutated"
    assert returned["event"] == "run_started"


def test_trace_listener_gets_redacted_tool_details_without_persisting_them(tmp_path):
    agent = _agent(tmp_path)
    agent.redaction_env = {"PONY_API_KEY": "secret-value"}
    received = []
    agent._trace_listener = received.append
    state = TaskState.create(run_id="run", task_id="task", user_request="test")
    agent.run_store.start_run(state)

    returned = agent.emit_trace(
        state,
        "tool_started",
        {
            "name": "run_shell",
            "tool_use_id": "tool_1",
            "args": {"command": "echo secret-value"},
        },
    )

    assert "args" not in returned
    assert "secret-value" not in str(received[0])
    assert received[0]["args"]["command"] == "echo <redacted>"
    persisted = json.loads(
        agent.run_store.trace_path(state).read_text(encoding="utf-8").splitlines()[-1]
    )
    assert "args" not in persisted

    agent.emit_trace(
        state,
        "tool_executed",
        {
            "name": "run_shell",
            "tool_use_id": "tool_1",
            "tool_status": "error",
            "result": "failed with secret-value",
        },
    )

    assert received[-1]["result"] == "failed with <redacted>"
    persisted = json.loads(
        agent.run_store.trace_path(state).read_text(encoding="utf-8").splitlines()[-1]
    )
    assert "result" not in persisted


def test_approval_prompt_is_used_when_installed(tmp_path):
    agent = _agent(tmp_path)
    received = []
    agent._approval_prompt = lambda name, args: received.append((name, args)) or True

    assert agent.approve("write_file", {"path": "README.md"}) is True
    assert received == [("write_file", {"path": "README.md"})]


def test_broken_approval_prompt_fails_closed(tmp_path):
    agent = _agent(tmp_path)
    agent._approval_prompt = lambda *_args: (_ for _ in ()).throw(RuntimeError())

    assert agent.approve("write_file", {"path": "README.md"}) is False
