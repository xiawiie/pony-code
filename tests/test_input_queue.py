import queue
import threading

from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.cli.input_queue import InputQueue, MAX_PENDING_INPUTS
from pony.cli.start import run_repl
from pony.runtime.options import RuntimeOptions
from pony.runtime.resume import active_prompt_history
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext


class _BlockingModelClient(FakeModelClient):
    def __init__(self, outputs):
        super().__init__(outputs)
        self.entered = [threading.Event() for _output in outputs]
        self.release = [threading.Event() for _output in outputs]

    def complete(self, **request):
        index = len(self.requests)
        self.entered[index].set()
        assert self.release[index].wait(timeout=3)
        return super().complete(**request)


def _agent(tmp_path, model_client):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pony(
        model_client=model_client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(project_trusted=True),
    )


def _start_plain_repl(agent, monkeypatch):
    inputs = queue.Queue()
    outcome = []
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt="": inputs.get(timeout=3),
    )

    def run():
        try:
            outcome.append(run_repl(agent, plain=True))
        except BaseException as exc:  # noqa: BLE001 - assert worker failures in test
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return inputs, outcome, thread


def test_input_queue_is_bounded_and_clear_drops_only_pending_inputs():
    entered = threading.Event()
    release = threading.Event()
    processed = []

    def process(text):
        processed.append(text)
        entered.set()
        assert release.wait(timeout=3)

    input_queue = InputQueue(process)

    assert input_queue.submit("active").status == "started"
    assert entered.wait(timeout=3)
    for index in range(MAX_PENDING_INPUTS):
        result = input_queue.submit(f"pending-{index}")
        assert result.status == "queued"
    assert input_queue.submit("overflow").status == "full"
    assert input_queue.clear() == MAX_PENDING_INPUTS

    release.set()
    input_queue.close()

    assert processed == ["active"]


def test_plain_repl_executes_queued_turns_in_canonical_order(
    tmp_path,
    monkeypatch,
    capsys,
):
    model_client = _BlockingModelClient(("first done", "second done"))
    agent = _agent(tmp_path, model_client)
    queued = threading.Event()
    from pony.cli import start as start_module

    route = start_module._route_repl_input

    def observe(*args, **kwargs):
        result = route(*args, **kwargs)
        if args[2] == "second request":
            queued.set()
        return result

    monkeypatch.setattr(start_module, "_route_repl_input", observe)
    inputs, outcome, thread = _start_plain_repl(agent, monkeypatch)

    inputs.put("first request")
    assert model_client.entered[0].wait(timeout=3)
    inputs.put("second request")
    assert queued.wait(timeout=3)
    model_client.release[0].set()
    assert model_client.entered[1].wait(timeout=3)
    inputs.put("/exit")
    model_client.release[1].set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert outcome == [0]
    assert active_prompt_history(agent.session["messages"]) == [
        "first request",
        "second request",
    ]
    assert "queued input: 1/5 pending" in capsys.readouterr().out


def test_queue_commands_are_zero_write_and_clear_unstarted_turn(
    tmp_path,
    monkeypatch,
):
    model_client = _BlockingModelClient(("done",))
    agent = _agent(tmp_path, model_client)
    before = len(agent.session_store.load_tree(agent.session["id"]).entries)
    cleared = threading.Event()
    counts = []
    from pony.cli import start as start_module

    route = start_module._route_repl_input

    def observe(*args, **kwargs):
        user_input = args[2]
        if user_input == "/queue clear":
            counts.append(
                len(agent.session_store.load_tree(agent.session["id"]).entries)
            )
        result = route(*args, **kwargs)
        if user_input == "/queue clear":
            counts.append(
                len(agent.session_store.load_tree(agent.session["id"]).entries)
            )
            cleared.set()
        return result

    monkeypatch.setattr(start_module, "_route_repl_input", observe)
    inputs, outcome, thread = _start_plain_repl(agent, monkeypatch)

    inputs.put("active request")
    assert model_client.entered[0].wait(timeout=3)
    inputs.put("never execute")
    inputs.put("/queue clear")
    assert cleared.wait(timeout=3)
    model_client.release[0].set()
    inputs.put("/exit")
    thread.join(timeout=3)

    assert outcome == [0]
    assert counts[0] == counts[1]
    assert counts[0] > before
    assert active_prompt_history(agent.session["messages"]) == ["active request"]
    assert all(
        entry["type"] != "input_queue"
        for entry in agent.session_store.load_tree(agent.session["id"]).entries
    )


def test_confirmation_input_is_not_added_to_the_pending_queue():
    confirmation_ready = threading.Event()
    decisions = []
    input_queue = None

    def process(_text):
        decisions.append(input_queue.confirm("Approve once? [y/N] "))

    input_queue = InputQueue(process, on_wake=confirmation_ready.set)
    input_queue.submit("active")
    assert confirmation_ready.wait(timeout=3)
    assert input_queue.answer_confirmation("yes") is True
    input_queue.close()

    assert decisions == [True]
    assert input_queue.pending_count == 0
