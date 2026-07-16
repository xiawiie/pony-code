"""Factory for Pico's internal model transport adapters."""


def build_model_client(
    client_kind,
    *,
    model,
    base_url,
    api_key,
    timeout,
    auth_mode,
    capabilities=None,
    temperature=None,
    compatibility="standard",
    top_p=0.9,
):
    """Build a client from an explicit protocol family without inference."""
    common = {
        "model": model,
        "api_key": api_key,
        "timeout": timeout,
        "auth_mode": auth_mode,
        "capabilities": dict(capabilities or {}),
    }
    if client_kind == "anthropic_messages":
        from .anthropic_compatible import AnthropicCompatibleModelClient

        return AnthropicCompatibleModelClient(
            base_url=base_url,
            temperature=temperature,
            **common,
        )
    if client_kind == "openai_responses":
        from .openai_compatible import OpenAICompatibleModelClient

        return OpenAICompatibleModelClient(
            base_url=base_url,
            temperature=temperature,
            **common,
        )
    if client_kind == "openai_chat_completions":
        from .openai_chat import OpenAIChatCompletionsModelClient

        return OpenAIChatCompletionsModelClient(
            base_url=base_url,
            temperature=temperature,
            compatibility=compatibility,
            **common,
        )
    if client_kind == "ollama_chat":
        from .ollama import OllamaModelClient

        return OllamaModelClient(
            host=base_url,
            temperature=0.0 if temperature is None else temperature,
            top_p=top_p,
            **common,
        )
    raise ValueError("unsupported model client kind")
