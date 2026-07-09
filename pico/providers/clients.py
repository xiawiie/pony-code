"""模型后端适配层。

runtime 只关心一件事：给我一个 prompt，我拿回一段文本。
不同 provider 在 HTTP 接口、响应结构、是否支持 prompt cache 上都有差异，
这些差异都在这里被抹平成统一的 complete() 接口。
"""

from ._shared import (
    _extract_usage_cache_details,  # noqa: F401
    _iter_sse_data_payloads,  # noqa: F401
)
from .anthropic_messages import (  # noqa: F401
    AnthropicMessagesAdapter,
    _anthropic_cache_control,
    _anthropic_no_text_error,
    _extract_anthropic_text,
    _extract_anthropic_usage_cache_details,
    _supports_anthropic_prompt_cache,
)
from .ollama_generate import OllamaGenerateAdapter  # noqa: F401
from .openai_chat import OpenAIChatAdapter  # noqa: F401
from .openai_responses import (  # noqa: F401
    OPENAI_COMPATIBLE_USER_AGENT,
    OpenAIResponsesAdapter,
    _extract_openai_response_from_sse,
    _extract_openai_text,
    _extract_openai_text_from_sse,
)

AnthropicCompatibleModelClient = AnthropicMessagesAdapter
OllamaModelClient = OllamaGenerateAdapter
OpenAICompatibleModelClient = OpenAIResponsesAdapter


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        if not getattr(self, "last_completion_metadata", None):
            self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)

    def stream_complete(self, prompt, max_new_tokens, **kwargs):
        yield self.complete(prompt, max_new_tokens, **kwargs)
