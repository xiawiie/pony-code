"""Factory for resolved model connection adapters."""

from .anthropic_messages import AnthropicMessagesAdapter
from .ollama_generate import OllamaGenerateAdapter
from .openai_chat import OpenAIChatAdapter
from .openai_responses import OpenAIResponsesAdapter


def build_model_client(resolved, *, temperature, top_p):
    if resolved.api == "openai-chat":
        return OpenAIChatAdapter(
            model=resolved.name,
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            temperature=temperature,
            timeout=resolved.timeout,
        )
    if resolved.api == "openai-responses":
        return OpenAIResponsesAdapter(
            model=resolved.name,
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            temperature=temperature,
            timeout=resolved.timeout,
        )
    if resolved.api == "anthropic-messages":
        return AnthropicMessagesAdapter(
            model=resolved.name,
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            temperature=temperature,
            timeout=resolved.timeout,
        )
    if resolved.api == "ollama":
        return OllamaGenerateAdapter(
            model=resolved.name,
            base_url=resolved.base_url,
            temperature=temperature,
            top_p=top_p,
            timeout=resolved.timeout,
        )
    raise ValueError(f"Unsupported model api {resolved.api!r}")
