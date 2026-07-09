import pytest

from pico.model_config import ModelConnection
from pico.model_resolver import ModelResolutionError, resolve_model_connection


def connection(name, base_url, api=None):
    return ModelConnection(
        name=name,
        base_url=base_url,
        api_key_env="TEST_API_KEY",
        api_key="sk-test",
        api=api,
        timeout=300,
    )


def test_resolves_dashscope_to_openai_chat():
    resolved = resolve_model_connection(
        connection("qwen-max", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    )

    assert resolved.api == "openai-chat"
    assert resolved.adapter_class == "OpenAIChatAdapter"


def test_resolves_bigmodel_to_openai_chat():
    resolved = resolve_model_connection(
        connection("glm-4.6", "https://open.bigmodel.cn/api/paas/v4")
    )

    assert resolved.api == "openai-chat"


def test_resolves_anthropic_path_to_anthropic_messages():
    resolved = resolve_model_connection(
        connection("deepseek-chat", "https://api.deepseek.com/anthropic")
    )

    assert resolved.api == "anthropic-messages"
    assert resolved.adapter_class == "AnthropicMessagesAdapter"


def test_resolves_ollama_host_to_ollama():
    resolved = resolve_model_connection(
        connection("qwen3:4b", "http://127.0.0.1:11434")
    )

    assert resolved.api == "ollama"
    assert resolved.adapter_class == "OllamaGenerateAdapter"


def test_resolves_openai_chat_completions_endpoint_to_openai_chat():
    resolved = resolve_model_connection(
        connection("gpt-4o", "https://api.openai.com/v1/chat/completions")
    )

    assert resolved.api == "openai-chat"
    assert resolved.adapter_class == "OpenAIChatAdapter"


def test_resolved_model_connection_repr_does_not_expose_api_key():
    resolved = resolve_model_connection(
        connection("gpt-5.4", "https://example.test/v1", api="openai-responses")
    )

    assert "sk-test" not in repr(resolved)


def test_explicit_api_wins_over_inference():
    resolved = resolve_model_connection(
        connection("gpt-5.4", "https://example.test/v1", api="openai-responses")
    )

    assert resolved.api == "openai-responses"
    assert resolved.adapter_class == "OpenAIResponsesAdapter"


def test_unknown_base_url_fails_with_fix():
    with pytest.raises(ModelResolutionError, match='api = "openai-chat"'):
        resolve_model_connection(
            connection("custom-model", "https://llm.example.test/v1")
        )
