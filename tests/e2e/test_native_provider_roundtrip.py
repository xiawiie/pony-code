import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pony import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from pony.providers.anthropic_messages import AnthropicMessagesModelClient
from pony.providers.ollama_chat import OllamaChatModelClient
from pony.providers.openai_chat_completions import OpenAIChatCompletionsModelClient
from pony.providers.openai_responses import OpenAIResponsesModelClient
from pony.runtime.options import RuntimeOptions


@contextmanager
def _fake_native_http_server(responses):
    captured = []
    queued = list(responses)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler contract
            length = int(self.headers.get("Content-Length", "0"))
            captured.append(
                {
                "path": self.path,
                "body": json.loads(self.rfile.read(length)),
                }
            )
            body = json.dumps(queued.pop(0)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-request-id", f"fake-{len(captured)}")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _tool_request(scenario):
    if scenario == "read_success":
        return "read_file", {"path": "README.md", "start": 1, "end": 1}
    if scenario == "write_denied":
        return "write_file", {"path": "blocked.txt", "content": "blocked\n"}
    return "read_file", {"path": "missing.txt", "start": 1, "end": 1}


def _provider_responses(family, scenario):
    name, arguments = _tool_request(scenario)
    if family == "anthropic":
        return [
            {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "use the requested tool",
                        "signature": "opaque-signature",
                    },
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": name,
                        "input": arguments,
                    },
                ],
                "usage": {},
            },
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "closed-loop-finished"}],
                "usage": {},
            },
        ]
    if family == "responses":
        return [
            {
                "id": "response-1",
                "output": [
                    {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": name,
                    "arguments": json.dumps(arguments),
                    }
                ],
                "usage": {},
            },
            {
                "id": "response-2",
                "output": [
                    {
                    "type": "message",
                        "content": [
                            {
                        "type": "output_text",
                        "text": "closed-loop-finished",
                            }
                        ],
                    }
                ],
                "usage": {},
            },
        ]
    if family == "chat":
        return [
            {
                "choices": [
                    {
                    "message": {
                        "role": "assistant",
                        "content": None,
                            "tool_calls": [
                                {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                                }
                            ],
                    },
                    "finish_reason": "tool_calls",
                    }
                ],
                "usage": {},
            },
            {
                "choices": [
                    {
                    "message": {
                        "role": "assistant",
                        "content": "closed-loop-finished",
                    },
                    "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
        ]
    return [
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                    "id": "call-1",
                    "function": {"name": name, "arguments": arguments},
                    }
                ],
            },
            "done": True,
            "done_reason": "stop",
        },
        {
            "message": {"role": "assistant", "content": "closed-loop-finished"},
            "done": True,
            "done_reason": "stop",
        },
    ]


def _native_client(family, root):
    if family == "anthropic":
        return AnthropicMessagesModelClient(
            model="anthropic-custom",
            base_url=root + "/anthropic/v9",
            api_key="",
            temperature=None,
            timeout=5,
            auth_mode="none",
            capabilities={},
        )
    if family == "responses":
        return OpenAIResponsesModelClient(
            model="openai-custom",
            base_url=root + "/openai/v9",
            api_key="",
            temperature=None,
            timeout=5,
            auth_mode="none",
            capabilities={},
        )
    if family == "chat":
        return OpenAIChatCompletionsModelClient(
            model="chat-custom",
            base_url=root + "/chat/v9",
            api_key="",
            temperature=None,
            timeout=5,
            auth_mode="none",
            capabilities={},
        )
    return OllamaChatModelClient(
        model="ollama-custom",
        host=root + "/ollama/v9",
        api_key="",
        temperature=0.0,
        top_p=0.9,
        timeout=5,
        auth_mode="none",
        capabilities={},
    )


def _tool_result_from_followup(family, body):
    if family == "anthropic":
        return body["messages"][-1]["content"][0]["content"]
    if family == "responses":
        return next(
            item["output"]
            for item in reversed(body["input"])
            if item.get("type") == "function_call_output"
        )
    if family == "chat":
        return body["messages"][-1]["content"]
    return body["messages"][-1]["content"]


@pytest.mark.parametrize(
    "family",
    ("anthropic", "responses", "chat", "ollama"),
)
@pytest.mark.parametrize(
    "scenario",
    ("read_success", "write_denied", "tool_error"),
)
def test_native_adapter_agent_loop_closes_two_round_tool_flow(
    tmp_path, family, scenario
):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    responses = _provider_responses(family, scenario)

    with _fake_native_http_server(responses) as (root, captured):
        client = _native_client(family, root)
        agent = Pony(
            model_client=client,
            workspace=WorkspaceContext.build(tmp_path),
            session_store=SessionStore(tmp_path / ".pony" / "sessions"),
            options=RuntimeOptions(project_trusted=True),
        )
        if scenario == "write_denied":
            agent.set_permission_mode("dontAsk")

        answer = agent.ask(f"Run the {scenario} protocol fixture")

    assert answer == "closed-loop-finished"
    assert len(captured) == 2
    expected_suffix = {
        "anthropic": "/messages",
        "responses": "/responses",
        "chat": "/chat/completions",
        "ollama": "/api/chat",
    }[family]
    assert [request["path"] for request in captured] == [
        {
            "anthropic": "/anthropic/v9/messages",
            "responses": "/openai/v9/responses",
            "chat": "/chat/v9/chat/completions",
            "ollama": "/ollama/v9/api/chat",
        }[family]
    ] * 2
    assert all(request["path"].endswith(expected_suffix) for request in captured)
    result = _tool_result_from_followup(family, captured[1]["body"])
    if scenario == "read_success":
        assert "demo" in result
    else:
        assert "error:" in result
    if family == "anthropic":
        assert captured[1]["body"]["messages"][-2]["content"][0]["type"] == ("thinking")
    assert not (tmp_path / "blocked.txt").exists()
