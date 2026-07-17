"""Factory for Pico's internal model transport adapters."""


def build_transport_client(
    transport_kind,
    *,
    model,
    base_url,
    api_key,
    timeout,
    auth_mode,
    capabilities=None,
    temperature=None,
    top_p=0.9,
):
    """Build a client from an explicit transport kind without inference."""
    common = {
        "model": model,
        "api_key": api_key,
        "timeout": timeout,
        "auth_mode": auth_mode,
        "capabilities": dict(capabilities or {}),
    }
    if transport_kind == "anthropic_messages":
        from .anthropic_messages import AnthropicMessagesModelClient

        return AnthropicMessagesModelClient(
            base_url=base_url,
            temperature=temperature,
            **common,
        )
    if transport_kind == "openai_responses":
        from .openai_responses import OpenAIResponsesModelClient

        return OpenAIResponsesModelClient(
            base_url=base_url,
            temperature=temperature,
            **common,
        )
    if transport_kind == "openai_chat_completions":
        from .openai_chat_completions import OpenAIChatCompletionsModelClient

        return OpenAIChatCompletionsModelClient(
            base_url=base_url,
            temperature=temperature,
            **common,
        )
    if transport_kind == "ollama_chat":
        from .ollama_chat import OllamaChatModelClient

        return OllamaChatModelClient(
            host=base_url,
            temperature=0.0 if temperature is None else temperature,
            top_p=top_p,
            **common,
        )
    raise ValueError("unsupported transport kind")
