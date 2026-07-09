"""Provider adapter exports."""

import json

from .anthropic_messages import AnthropicMessagesAdapter
from .message_utils import strip_pico_meta
from .ollama_generate import OllamaGenerateAdapter
from .openai_chat import OpenAIChatAdapter
from .openai_responses import OpenAIResponsesAdapter
from .response import Response, StopReason

__all__ = [
    "AnthropicMessagesAdapter",
    "FakeModelClient",
    "OllamaGenerateAdapter",
    "OpenAIChatAdapter",
    "OpenAIResponsesAdapter",
]


def _fake_content_to_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in {"text", "input_text", "output_text"} or "text" in block:
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
            continue
        if block_type == "tool_use":
            name = str(block.get("name", "") or "")
            raw_input = block.get("input", {})
            if isinstance(raw_input, dict):
                rendered_input = json.dumps(raw_input, sort_keys=True)
            else:
                rendered_input = str(raw_input or "")
            if name:
                parts.append(f"{name}({rendered_input})")
            elif rendered_input:
                parts.append(rendered_input)
            continue
        if block_type == "tool_result":
            result = block.get("content", "")
            if isinstance(result, list):
                text = _fake_content_to_text(result)
            elif isinstance(result, str):
                text = result
            elif isinstance(result, dict):
                text = json.dumps(result, sort_keys=True)
            else:
                text = str(result or "")
            if text:
                parts.append(text)
    return "\n".join(parts)


def _fake_prompt_from_v2(system, messages):
    parts = []
    system_text = _fake_content_to_text(system)
    if system_text:
        parts.append(system_text)
    for message in strip_pico_meta(messages):
        text = _fake_content_to_text(message.get("content", ""))
        if text:
            parts.append(f"[{message.get('role', 'user')}] {text}")
    return "\n\n".join(parts)


class FakeModelClient:
    supports_prompt_cache = False
    supports_native_tools = False
    last_completion_metadata = {}

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.last_completion_metadata = {}

    def complete_v2(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        del tools, max_tokens, cache_breakpoints
        prompt = _fake_prompt_from_v2(system, messages)
        self.prompts.append(prompt)
        self.last_completion_metadata = {}
        text = self._next_output_text(prompt)
        return Response(
            stop_reason=StopReason.END_TURN,
            content=[{"type": "text", "text": str(text)}],
            usage={},
        )

    def _next_output_text(self, prompt):
        del prompt
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)
