"""agent_loop 在 v2 路径下正确 append messages。"""
from unittest.mock import MagicMock


def _stub_agent_loop_deps(agent):
    # 简化：mock 出所有非核心方法，只测 message 追加形状
    agent.session = {"messages": [], "id": "s1"}
    agent.record_message = MagicMock(side_effect=lambda m: agent.session["messages"].append(m))
    agent.workspace = MagicMock()
    agent.workspace.repo_root = "/tmp"


def test_agent_loop_appends_user_message_at_start():
    from pico.agent_loop import _append_user_turn

    agent = MagicMock()
    _stub_agent_loop_deps(agent)
    _append_user_turn(agent, "hello world")
    msgs = agent.session["messages"]
    assert msgs[-1] == {
        "role": "user",
        "content": "hello world",
        "_pico_meta": {"created_at": msgs[-1]["_pico_meta"]["created_at"]},
    }


def test_agent_loop_appends_tool_use_and_tool_result_pair():
    from pico.agent_loop import _append_tool_result, _append_tool_use

    agent = MagicMock()
    _stub_agent_loop_deps(agent)
    tool_use_id = _append_tool_use(agent, name="read_file", input={"path": "a.py"}, id_hint="toolu_x")
    _append_tool_result(agent, tool_use_id=tool_use_id, content="file text")

    msgs = agent.session["messages"]
    assert msgs[-2]["role"] == "assistant"
    assert msgs[-2]["content"][0]["type"] == "tool_use"
    assert msgs[-2]["content"][0]["id"] == "toolu_x"
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"][0]["type"] == "tool_result"
    assert msgs[-1]["content"][0]["tool_use_id"] == "toolu_x"
    assert msgs[-1]["content"][0]["content"] == "file text"
