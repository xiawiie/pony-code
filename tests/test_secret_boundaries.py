import json
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

from pico import Pico, SessionStore, WorkspaceContext
from pico import security as securitylib
from pico.cli_start import run_agent_once
from pico.messages import validate_messages
from pico.providers.response import Response, StopReason
from pico.security import SensitiveDataBlockedError


class CapturingClient:
    supports_prompt_cache = False
    supports_native_tools = True

    def __init__(self, response):
        self.response = response
        self.requests = []

    def complete_v2(self, **request):
        self.requests.append(request)
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response


class RaisingClient(CapturingClient):
    def complete_v2(self, **request):
        self.requests.append(request)
        raise self.response


def final_response(text):
    return Response(
        stop_reason=StopReason.END_TURN,
        content=[{"type": "text", "text": text}],
    )


def build_agent_with_client(tmp_path, client):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        max_steps=2,
    )


def all_normal_artifact_bytes(root):
    chunks = []
    for path in (Path(root) / ".pico").rglob("*"):
        if (
            path.is_file()
            and "/backup/" not in path.as_posix()
            and "/blobs/" not in path.as_posix()
        ):
            chunks.append(path.read_bytes())
    return b"\n".join(chunks)


def test_configured_env_name_does_not_reclassify_generic_artifact_key():
    env = {
        "PATH": "/private/toolchain",
        "CUSTOM_OPAQUE": "opaque-configured-value",
    }
    secret_env_names = {"PATH", "CUSTOM_OPAQUE"}

    redacted = securitylib.redact_artifact(
        {
            "path": "README.md",
            "path_text": "/private/toolchain",
            "custom_text": "opaque-configured-value",
        },
        env=env,
        secret_env_names=secret_env_names,
    )

    assert redacted["path"] == "README.md"
    assert redacted["path_text"] == "<redacted>"
    assert redacted["custom_text"] == "<redacted>"


def test_provider_request_sanitizes_system_messages_and_injection(tmp_path):
    secret = "github_pat_A123456789012345678901234567890"
    client = CapturingClient(final_response("done"))
    agent = build_agent_with_client(tmp_path, client)
    agent.prefix += "\n" + secret
    agent.session["working_memory"]["task_summary"] = secret

    agent.ask("token budget")

    assert secret not in json.dumps(client.requests)
    assert secret in agent.prefix


def test_secret_tool_action_is_rejected_before_runner(tmp_path):
    secret = "sk-tool-action-secret-123456789"
    response = Response(
        stop_reason=StopReason.TOOL_USE,
        content=[{
            "type": "tool_use",
            "id": "toolu_1",
            "name": "write_file",
            "input": {"path": "x.txt", "content": secret},
        }],
    )
    agent = build_agent_with_client(
        tmp_path,
        CapturingClient([response, final_response("done")]),
    )
    runner = Mock(return_value="must not run")
    agent.tools["write_file"]["run"] = runner

    result = agent.ask("write it")

    assert secret not in result
    runner.assert_not_called()


def test_opaque_secret_mapping_value_in_action_is_rejected_as_native_pair(tmp_path):
    response = Response(
        stop_reason=StopReason.TOOL_USE,
        content=[{
            "type": "tool_use",
            "id": "toolu_opaque",
            "name": "write_file",
            "input": {
                "path": "x.txt",
                "content": "safe",
                "credential": "opaque-value",
            },
        }],
    )
    agent = build_agent_with_client(
        tmp_path,
        CapturingClient([response, final_response("done")]),
    )
    runner = Mock(return_value="must not run")
    agent.tools["write_file"]["run"] = runner

    assert agent.ask("write it") == "done"

    runner.assert_not_called()
    validate_messages(agent.session["messages"], require_meta=True)
    tool_use, tool_result = agent.session["messages"][1:3]
    assert tool_use["content"][0]["id"] == "toolu_opaque"
    assert tool_use["content"][0]["input"]["credential"] == "<redacted>"
    assert tool_result["content"][0]["tool_use_id"] == "toolu_opaque"
    assert tool_result["_pico_meta"]["tool_status"] == "rejected"
    assert tool_result["_pico_meta"]["effect_class"] == "workspace_write"
    assert tool_result["_pico_meta"]["tool_error_code"] == (
        "sensitive_content_block"
    )
    assert tool_result["_pico_meta"]["security_event_type"] == (
        "sensitive_access_block"
    )
    assert agent._last_tool_result_metadata["tool_error_code"] == (
        "sensitive_content_block"
    )


def test_provider_final_and_cli_output_never_print_secret(tmp_path, capsys):
    secret = "github_pat_A123456789012345678901234567890"
    agent = build_agent_with_client(
        tmp_path,
        CapturingClient(final_response(secret)),
    )

    assert run_agent_once(agent, ["answer"]) == 0

    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert secret.encode() not in all_normal_artifact_bytes(tmp_path)


def test_retry_excerpt_is_redacted_before_trace_and_next_request(tmp_path):
    secret = "github_pat_" + "R" * 32
    client = CapturingClient([
        Response(
            stop_reason=StopReason.STOP_SEQUENCE,
            content=[{"type": "text", "text": secret}],
        ),
        final_response("done"),
    ])
    agent = build_agent_with_client(tmp_path, client)

    assert agent.ask("retry safely") == "done"

    assert secret not in json.dumps(client.requests)
    assert secret.encode() not in all_normal_artifact_bytes(tmp_path)


def test_cli_and_agent_loop_bound_provider_error_text(tmp_path, capsys, caplog):
    secret = "github_pat_" + "E" * 32
    credential_url = f"https://user:{secret}@example.test/v1?api_key={secret}"
    error = RuntimeError(credential_url + " " + ("x" * 500))
    client = RaisingClient(error)
    agent = build_agent_with_client(tmp_path, client)
    caplog.set_level(logging.DEBUG, logger="pico")

    assert run_agent_once(agent, ["fail"]) == 1

    captured = capsys.readouterr()
    visible = captured.out + captured.err + caplog.text
    assert secret not in visible
    assert credential_url not in visible
    assert max((len(line) for line in captured.err.splitlines()), default=0) <= 300
    artifacts = all_normal_artifact_bytes(tmp_path)
    assert secret.encode() not in artifacts
    assert credential_url.encode() not in artifacts


def test_provider_residual_scan_blocks_when_primary_sanitizer_misses(
    tmp_path,
    monkeypatch,
    caplog,
):
    secret = "github_pat_" + "A" * 32
    client = CapturingClient(final_response("must not run"))
    agent = build_agent_with_client(tmp_path, client)
    agent.prefix = secret
    monkeypatch.setattr(securitylib, "redact_artifact", lambda value, **kwargs: value)

    with pytest.raises(SensitiveDataBlockedError):
        agent.ask("continue")

    assert client.requests == []
    assert secret not in caplog.text
    assert secret.encode() not in all_normal_artifact_bytes(tmp_path)
