import pytest

from pico.model_resolver import ResolvedModelConnection
from pico.providers.anthropic_messages import AnthropicMessagesAdapter
from pico.providers.factory import build_model_client
from pico.providers.ollama_generate import OllamaGenerateAdapter
from pico.providers.openai_chat import OpenAIChatAdapter
from pico.providers.openai_responses import OpenAIResponsesAdapter


def _resolved(api):
    return ResolvedModelConnection(
        name="model-test",
        base_url="https://api.example.test/v1",
        api_key_env="TEST_API_KEY",
        api_key="sk-test",
        api=api,
        adapter_class="",
        timeout=30,
    )


def test_build_model_client_returns_openai_chat_adapter():
    client = build_model_client(_resolved("openai-chat"), temperature=0.2, top_p=0.9)

    assert isinstance(client, OpenAIChatAdapter)


def test_build_model_client_returns_openai_responses_adapter():
    client = build_model_client(_resolved("openai-responses"), temperature=0.2, top_p=0.9)

    assert isinstance(client, OpenAIResponsesAdapter)


def test_build_model_client_returns_anthropic_messages_adapter():
    client = build_model_client(_resolved("anthropic-messages"), temperature=0.2, top_p=0.9)

    assert isinstance(client, AnthropicMessagesAdapter)


def test_build_model_client_returns_ollama_generate_adapter():
    resolved = _resolved("ollama")
    client = build_model_client(resolved, temperature=0.2, top_p=0.9)

    assert isinstance(client, OllamaGenerateAdapter)


def test_build_model_client_rejects_unsupported_api():
    with pytest.raises(ValueError, match="Unsupported model api"):
        build_model_client(_resolved("unknown-api"), temperature=0.2, top_p=0.9)
