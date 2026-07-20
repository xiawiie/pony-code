from contextlib import contextmanager
import threading
from unittest.mock import Mock

from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.runtime.options import RuntimeOptions
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext


def _agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )
    agent.set_permission_mode("default")
    return agent


def test_approval_finishes_before_host_mutation_lock(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    events = []

    @contextmanager
    def mutation_lock(path, *, require_lock):
        assert path == agent.mutation_lock_path
        assert require_lock is True
        events.append("lock")
        yield

    monkeypatch.setattr("pony.tools.executor.locked_file", mutation_lock)
    agent.approve = Mock(side_effect=lambda *_args: events.append("approval") or True)
    agent.tools["write_file"]["run"] = Mock(
        side_effect=lambda _args: events.append("runner") or "written"
    )

    result = agent.execute_tool(
        "write_file",
        {"path": "note.txt", "content": "hello\n"},
    )

    assert result.metadata["tool_status"] == "ok"
    assert events == ["approval", "lock", "runner"]


def test_host_mutation_lock_covers_runner_and_effect_observation(
    tmp_path, monkeypatch
):
    agent = _agent(tmp_path)
    lock_active = False

    @contextmanager
    def mutation_lock(_path, *, require_lock):
        nonlocal lock_active
        assert require_lock is True
        lock_active = True
        try:
            yield
        finally:
            lock_active = False

    monkeypatch.setattr("pony.tools.executor.locked_file", mutation_lock)
    original_end = agent.workspace_observer.capture_call_end
    agent.workspace_observer.capture_call_end = Mock(
        side_effect=lambda: (
            (_ for _ in ()).throw(AssertionError("observer ran without lock"))
            if not lock_active
            else original_end()
        )
    )

    def write(_args):
        assert lock_active is True
        (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
        return "written"

    agent.approve = Mock(return_value=True)
    agent.tools["write_file"]["run"] = Mock(side_effect=write)

    result = agent.execute_tool(
        "write_file",
        {"path": "note.txt", "content": "hello\n"},
    )

    assert result.metadata["affected_paths"] == ["note.txt"]
    assert result.metadata["diff_summary"] == ["created:note.txt"]
    assert lock_active is False


def test_host_mutation_locks_do_not_serialize_separate_worktrees(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = _agent(first_root)
    second = _agent(second_root)
    first.approve = Mock(return_value=True)
    second.approve = Mock(return_value=True)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    errors = []

    def write_first(args):
        (first_root / args["path"]).write_text(args["content"], encoding="utf-8")
        first_entered.set()
        assert release_first.wait(timeout=5)
        return "written"

    def write_second(args):
        (second_root / args["path"]).write_text(args["content"], encoding="utf-8")
        return "written"

    first.tools["write_file"]["run"] = write_first
    second.tools["write_file"]["run"] = write_second

    def run(agent, path, *, finished=None):
        try:
            result = agent.execute_tool("write_file", {"path": path, "content": "ok\n"})
            assert result.metadata["tool_status"] == "ok"
        except Exception as exc:  # pragma: no cover - asserted after both threads join.
            errors.append(exc)
        finally:
            if finished is not None:
                finished.set()

    first_thread = threading.Thread(target=run, args=(first, "first.txt"))
    second_thread = threading.Thread(
        target=run,
        args=(second, "second.txt"),
        kwargs={"finished": second_finished},
    )
    first_thread.start()
    assert first_entered.wait(timeout=5)
    second_thread.start()
    assert second_finished.wait(timeout=2)
    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not errors
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
