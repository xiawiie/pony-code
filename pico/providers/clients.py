"""模型后端适配层。

runtime 只关心一件事：给我一个 prompt，我拿回一段文本。
不同 provider 在 HTTP 接口、响应结构、是否支持 prompt cache 上都有差异，
这些差异都在这里被抹平成统一的 complete() 接口。
"""

from ._shared import (
    _extract_usage_cache_details,  # noqa: F401
    _iter_sse_data_payloads,  # noqa: F401
)
from .anthropic_compatible import (  # noqa: F401
    AnthropicCompatibleModelClient,
    _extract_anthropic_usage_cache_details,
    _supports_anthropic_prompt_cache,
)
from .fake import FakeModelClient  # noqa: F401
from .ollama import OllamaModelClient  # noqa: F401
from .openai_compatible import (  # noqa: F401
    OPENAI_COMPATIBLE_USER_AGENT,
    OpenAICompatibleModelClient,
    _extract_openai_response_from_sse,
    _extract_openai_text,
    _extract_openai_text_from_sse,
)
