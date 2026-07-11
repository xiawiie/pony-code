import hashlib

import pytest

from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient
from pico.context.renderer import render_current_user_message
from pico.context_manager import ContextManager, _build_tools_list


def _agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )


def _build_request(agent, user_message):
    agent.session["messages"].append(
        {"role": "user", "content": user_message, "_pico_meta": {}}
    )
    snapshot, telemetry = render_current_user_message(agent, user_message)
    return ContextManager(agent).build_request(
        injection_snapshot=snapshot,
        injection_telemetry=telemetry,
        preflight_metadata={},
    )


def test_pinned_system_and_tools_overflow_fails_loudly(tmp_path):
    agent = _agent(tmp_path)
    agent.prefix = "x" * 200_000

    with pytest.raises(RuntimeError, match="SystemTooBig"):
        _build_request(agent, "hi")


def test_tool_schema_keeps_integer_and_risk_contract():
    tools = {
        "write_file": {
            "schema": {"path": "str", "retries": "int=1"},
            "risky": True,
            "description": "Write a file.",
        }
    }

    converted = _build_tools_list(tools)[0]

    assert converted["input_schema"]["properties"]["retries"]["type"] == "integer"
    assert converted["input_schema"]["required"] == ["path"]
    assert "approval" in converted["description"].lower()


def test_system_prefix_hash_depends_on_stable_prefix_only(tmp_path):
    agent = _agent(tmp_path)
    first_request, first_metadata = _build_request(agent, "first")
    agent.session["messages"] = []
    agent.workspace.branch = "feature/request-view"
    agent.workspace.status = " M README.md"
    second_request, second_metadata = _build_request(agent, "second")

    assert first_request["system"][0]["text"] == second_request["system"][0]["text"]
    assert first_metadata["system_prefix_hash"] == second_metadata["system_prefix_hash"]
    assert first_metadata["system_prefix_hash"] == hashlib.sha256(
        agent.prefix.encode("utf-8")
    ).hexdigest()


def test_message_drop_uses_actual_request_messages(tmp_path):
    agent = _agent(tmp_path)
    agent.context_config["history_soft_cap"] = 500
    agent.context_config["history_floor_messages"] = 3
    agent.session["messages"] = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"old-{index}-" + ("x" * 300),
            "_pico_meta": {},
        }
        for index in range(10)
    ]

    request, metadata = _build_request(agent, "current")

    assert metadata["dropped_messages"] > 0
    assert metadata["messages_count"] == len(request["messages"])
    assert metadata["messages_chars"] == sum(
        len(str(message["content"])) for message in request["messages"]
    )
    assert request["messages"][-1]["content"].endswith("current")


def test_request_metrics_and_cache_key_use_sanitized_payload(tmp_path):
    secret = "github_pat_" + "M" * 32
    agent = _agent(tmp_path)
    agent.prefix += "\n" + secret
    counted = []

    def count_tokens(text):
        counted.append(str(text))
        return max(1, len(str(text)) // 4)

    agent.model_client.count_tokens = count_tokens

    request, metadata = _build_request(agent, "remember " + secret)

    sent_system = request["system"][0]["text"]
    assert secret not in sent_system
    assert secret not in str(request["messages"])
    assert secret not in "\n".join(counted)
    assert metadata["system_prefix_hash"] == hashlib.sha256(
        sent_system.encode("utf-8")
    ).hexdigest()
