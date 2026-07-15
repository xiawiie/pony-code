from copy import deepcopy

from tests.test_context_manager import _agent, _build_request


def test_context_snapshot_build_does_not_mutate_canonical_messages(tmp_path):
    agent = _agent(tmp_path)
    before = deepcopy(agent.session["messages"])
    request, _ = _build_request(agent, "current")
    assert request["messages"]
    assert agent.session["messages"][:-1] == before
