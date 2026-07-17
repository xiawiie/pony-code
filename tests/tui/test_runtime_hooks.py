from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.runtime.options import RuntimeOptions
from pony.state.session_store import SessionStore
from pony.state.task_state import TaskState
from pony.workspace.context import WorkspaceContext


def _agent(tmp_path, *, approval_policy="ask"):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(approval_policy=approval_policy),
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


def test_approval_prompt_is_used_only_for_ask_policy(tmp_path):
    agent = _agent(tmp_path)
    received = []
    agent._approval_prompt = lambda name, args: received.append((name, args)) or True

    assert agent.approve("write_file", {"path": "README.md"}) is True
    assert received == [("write_file", {"path": "README.md"})]

    agent.approval_policy = "never"
    assert agent.approve("write_file", {"path": "README.md"}) is False
    assert len(received) == 1


def test_broken_approval_prompt_fails_closed(tmp_path):
    agent = _agent(tmp_path)
    agent._approval_prompt = lambda *_args: (_ for _ in ()).throw(RuntimeError())

    assert agent.approve("write_file", {"path": "README.md"}) is False
