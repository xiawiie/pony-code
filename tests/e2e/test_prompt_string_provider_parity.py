"""E2E: native messages and prompt-string fake both see context injection."""

from pico.providers.clients import FakeModelClient
from pico.providers.response import Response, StopReason
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


class _SniffProvider:
    """Native v2 provider — records raw messages arg."""
    supports_prompt_cache = False
    supports_native_tools = True

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.last_completion_metadata = {}

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.calls.append({"messages": [dict(m) for m in messages]})
        return self.script.pop(0)


def test_native_and_prompt_string_fake_both_complete_a_final_turn(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)

    native = _SniffProvider([
        Response(stop_reason=StopReason.END_TURN, content=[{"type": "text", "text": "ok"}], usage={}),
    ])
    store1 = SessionStore(tmp_path / ".pico" / "sessions_a")
    pico_native = Pico(model_client=native, workspace=workspace, session_store=store1, max_steps=3)
    answer_native = pico_native.ask("hello world")

    prompt_string = FakeModelClient(["<final>ok</final>"])
    store2 = SessionStore(tmp_path / ".pico" / "sessions_b")
    pico_prompt_string = Pico(model_client=prompt_string, workspace=workspace, session_store=store2, max_steps=3)
    answer_prompt_string = pico_prompt_string.ask("hello world")

    assert answer_native.strip() == "ok"
    assert answer_prompt_string.strip() == "ok"

    native_content = native.calls[0]["messages"][-1]["content"]
    assert "<pico:workspace_state>" in native_content or "<system-reminder>" in native_content

    flattened_prompt = prompt_string.prompts[0]
    assert "<pico:workspace_state>" in flattened_prompt or "<system-reminder>" in flattened_prompt
