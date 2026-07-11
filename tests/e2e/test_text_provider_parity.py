"""E2E: same pico.ask input produces equivalent flow via native and text protocol paths."""

from pico.providers.text_protocol_adapter import TextProtocolAdapter
from pico.providers.response import Response, StopReason
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


class _SniffProvider:
    """Structured provider — records raw messages arg."""
    supports_prompt_cache = False

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.last_completion_metadata = {}

    def complete(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.calls.append({"messages": [dict(m) for m in messages]})
        return self.script.pop(0)


class _XmlStubInner:
    """Text transport used by TextProtocolAdapter."""
    def __init__(self, script):
        self.script = list(script)
        self.prompts = []
        self.last_completion_metadata = {}

    def complete_text(self, prompt, max_tokens):
        del max_tokens
        self.prompts.append(prompt)
        return self.script.pop(0)


def test_native_and_text_protocol_both_complete_a_final_turn(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)

    # 1. Native path
    native = _SniffProvider(
        [
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_native",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    }
                ],
                usage={},
            ),
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "ok"}],
                usage={},
            ),
        ]
    )
    store1 = SessionStore(tmp_path / ".pico" / "sessions_a")
    pico_native = Pico(model_client=native, workspace=workspace, session_store=store1, max_steps=3)
    answer_native = pico_native.ask("hello world")

    # 2. Text protocol path
    inner = _XmlStubInner(
        [
            '<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>',
            "<final>ok</final>",
        ]
    )
    text_adapter = TextProtocolAdapter(inner)
    text_store = SessionStore(tmp_path / ".pico" / "sessions_b")
    pico_text = Pico(model_client=text_adapter, workspace=workspace, session_store=text_store, max_steps=3)
    answer_text = pico_text.ask("hello world")

    assert answer_native.strip() == "ok"
    assert answer_text.strip() == "ok"
    assert pico_native.current_task_state.tool_steps == 1
    assert pico_text.current_task_state.tool_steps == 1

    native_events = [
        event
        for event in pico_native.run_store.trace_path(pico_native.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
        if '"event": "action_decoded"' in event
    ]
    text_events = [
        event
        for event in pico_text.run_store.trace_path(pico_text.current_task_state)
        .read_text(encoding="utf-8")
        .splitlines()
        if '"event": "action_decoded"' in event
    ]
    assert '"origin": "native_tool_use"' in native_events[0]
    assert '"origin": "text_protocol"' in text_events[0]

    # Native path saw <pico:*> blocks in messages.
    native_content = native.calls[0]["messages"][-1]["content"]
    assert "<pico:workspace_state>" in native_content or "<system-reminder>" in native_content

    # Text protocol path saw the same blocks after flattening.
    flattened_prompt = inner.prompts[0]
    assert "<pico:workspace_state>" in flattened_prompt or "<system-reminder>" in flattened_prompt
