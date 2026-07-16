import pytest

from tests.test_context_manager import _agent, _build_request


def test_required_context_overflow_fails_before_provider(tmp_path):
    agent = _agent(tmp_path)
    agent.prefix = "x" * 200_000
    with pytest.raises(RuntimeError, match="SystemContextTooLarge"):
        _build_request(agent, "latest user")
