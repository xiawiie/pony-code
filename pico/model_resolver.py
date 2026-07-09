"""Resolve model connection config into provider adapter metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pico.model_config import ModelConnection


SUPPORTED_APIS = {
    "openai-chat": "OpenAIChatAdapter",
    "openai-responses": "OpenAIResponsesAdapter",
    "anthropic-messages": "AnthropicMessagesAdapter",
    "ollama": "OllamaGenerateAdapter",
}


class ModelResolutionError(ValueError):
    """Raised when a model connection cannot be resolved to a supported API."""


@dataclass(frozen=True)
class ResolvedModelConnection:
    name: str
    base_url: str
    api_key_env: str
    api_key: str = field(repr=False)
    api: str
    adapter_class: str
    timeout: int
    native_tools: bool = False
    prompt_cache: bool = False


def resolve_model_connection(config: ModelConnection) -> ResolvedModelConnection:
    api = (config.api or "").strip() or infer_api(config.base_url, config.name)
    if api not in SUPPORTED_APIS:
        choices = ", ".join(sorted(SUPPORTED_APIS))
        raise ModelResolutionError(f"Unsupported model api {api!r}; choose one of: {choices}")

    return ResolvedModelConnection(
        name=config.name,
        base_url=config.base_url,
        api_key_env=config.api_key_env,
        api_key=config.api_key,
        api=api,
        adapter_class=SUPPORTED_APIS[api],
        timeout=config.timeout,
        native_tools=api == "anthropic-messages",
        prompt_cache=_supports_prompt_cache(api, config.base_url),
    )


def infer_api(base_url, model_name):
    parsed = urlsplit(base_url)
    host = (parsed.hostname or "").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.lower().rstrip("/")
    model = model_name.lower()

    if _is_local_ollama(parsed, host, netloc) or "ollama" in host:
        return "ollama"
    if "anthropic" in host or _path_mentions(path, "anthropic"):
        return "anthropic-messages"
    if "dashscope" in host:
        return "openai-chat"
    if "bigmodel" in host or "zhipuai" in host:
        return "openai-chat"
    if "openrouter" in host or "siliconflow" in host:
        return "openai-chat"
    if "volces" in host or host == "ark.cn-beijing.volces.com":
        return "openai-chat"
    if path.endswith("/chat/completions"):
        return "openai-chat"
    if host == "api.openai.com" and _path_is_v1(path):
        return "openai-responses"
    if model.startswith(("qwen", "glm-", "deepseek")) and path.endswith("/v1"):
        return "openai-chat"

    raise ModelResolutionError(
        f"Could not infer model api for {base_url!r}. Add api = \"openai-chat\" under [model]."
    )


def _is_local_ollama(parsed, host, netloc):
    if host in {"localhost", "127.0.0.1"} and parsed.port == 11434:
        return True
    return netloc in {"localhost:11434", "127.0.0.1:11434"}


def _path_mentions(path, name):
    return path == f"/{name}" or f"/{name}/" in f"{path}/"


def _path_is_v1(path):
    return path == "/v1" or path.startswith("/v1/")


def _supports_prompt_cache(api, base_url):
    normalized = base_url.lower()
    if api == "anthropic-messages":
        return "anthropic.com" in normalized or "right.codes" in normalized
    if api == "openai-responses":
        return "openai.com" in normalized or "right.codes" in normalized
    return False
