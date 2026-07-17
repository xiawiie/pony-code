"""Provider specifications and environment-to-Transport resolution."""

import ipaddress
import os
import urllib.parse


MODEL_ENV_NAME = "PICO_MODEL"
API_BASE_ENV_NAME = "PICO_API_BASE"
API_KEY_ENV_NAME = "PICO_API_KEY"
DEFAULT_PROVIDER = "anthropic"
SUPPORTED_PROVIDERS = ("anthropic", "openai", "ollama")

_PROVIDER_SPECS = {
    "anthropic": {
        "model": "claude-sonnet-4-6",
        "base_url": "https://api.anthropic.com/v1",
        "auth_mode": "x-api-key",
        "variants": {
            "messages": {
                "protocol": "anthropic_messages",
                "capabilities": {
                    "prompt_cache": True,
                    "strict_tools": True,
                    "parallel_tool_control": True,
                },
            },
        },
    },
    "openai": {
        "model": "gpt-5.4",
        "base_url": "https://api.openai.com/v1",
        "auth_mode": "bearer",
        "variants": {
            "responses": {
                "protocol": "openai_responses",
                "capabilities": {
                    "strict_tools": True,
                    "parallel_tool_control": True,
                    "reasoning_replay": True,
                },
            },
            "chat_completions": {
                "protocol": "openai_chat_completions",
                "capabilities": {
                    "strict_tools": True,
                    "parallel_tool_control": True,
                },
            },
        },
    },
    "ollama": {
        "model": "qwen3:8b",
        "base_url": "http://127.0.0.1:11434",
        "auth_mode": "none",
        "variants": {
            "chat": {
                "protocol": "ollama_chat",
                "capabilities": {},
            },
        },
    },
}

DEFAULT_MODEL = _PROVIDER_SPECS[DEFAULT_PROVIDER]["model"]
DEFAULT_API_BASE = _PROVIDER_SPECS[DEFAULT_PROVIDER]["base_url"]
_SECRET_QUERY_KEYS = {
    "api_key",
    "access_key",
    "access_token",
    "auth_token",
    "token",
    "secret",
    "password",
    "credential",
}


def validate_api_base(value):
    raw = str(value or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("api_base_invalid")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("api_base_invalid") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("api_base_credentials")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.casefold().replace("-", "_") in _SECRET_QUERY_KEYS for key, _ in query):
        raise ValueError("api_base_credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("api_base_query_or_fragment")
    if parsed.scheme.casefold() != "https" and not _loopback_api_url(raw):
        raise ValueError("insecure_api_base")
    return raw.rstrip("/")


def _resolve_env_value(name, project_env, process_env, default="", default_name=""):
    for source_name, source in (
        ("project_env", project_env),
        ("environment", process_env),
    ):
        if name in source:
            return {"value": source[name], "source": source_name, "name": name}
    if default:
        return {"value": default, "source": "default", "name": default_name}
    return {"value": "", "source": "unset", "name": ""}


def _resolve_required_setting(
    name,
    project_env,
    process_env,
    *,
    required,
    default,
    default_name,
    missing_error,
):
    setting = _resolve_env_value(name, project_env, process_env)
    if str(setting["value"] or "").strip():
        return setting
    if required:
        raise ValueError(missing_error)
    return {"value": default, "source": "default", "name": default_name}


def _loopback_api_url(value):
    parsed = urllib.parse.urlsplit(str(value))
    host = (parsed.hostname or "").casefold()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _route_api_base(base_url):
    parsed = urllib.parse.urlsplit(base_url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/")
    if host == "api.anthropic.com":
        return "anthropic", "messages"
    if host == "api.openai.com":
        return "openai", "responses"
    if _loopback_api_url(base_url) and path != "/v1":
        return "ollama", "chat"
    return "openai", "chat_completions"


def resolve_model_config(*, project_env=None, process_env=None, required=True):
    """Resolve Pico's Transport from its three generic environment variables."""
    project_env = dict(project_env or {})
    process_env = dict(os.environ if process_env is None else process_env)

    api_base = _resolve_required_setting(
        API_BASE_ENV_NAME,
        project_env,
        process_env,
        required=required,
        default=DEFAULT_API_BASE,
        default_name="anthropic_default_api_base",
        missing_error="api_base_not_configured",
    )
    api_base["value"] = validate_api_base(api_base["value"])
    provider_name, variant_name = _route_api_base(api_base["value"])
    spec = _PROVIDER_SPECS[provider_name]
    variant = spec["variants"][variant_name]
    provider = {
        "value": provider_name,
        "source": api_base["source"],
        "name": api_base["name"],
    }

    model = _resolve_required_setting(
        MODEL_ENV_NAME,
        project_env,
        process_env,
        required=required,
        default=spec["model"],
        default_name=f"{provider_name}_default_model",
        missing_error="model_not_configured",
    )
    model["value"] = str(model["value"]).strip()
    if not model["value"]:
        raise ValueError("model_invalid")

    api_variant = {
        "value": variant_name,
        "source": api_base["source"],
        "name": api_base["name"],
    }
    auth_mode = {
        "value": spec["auth_mode"],
        "source": api_base["source"],
        "name": api_base["name"],
    }

    api_key = _resolve_env_value(API_KEY_ENV_NAME, project_env, process_env)
    key_required = auth_mode["value"] != "none"
    if required and key_required and not api_key["value"]:
        raise ValueError("api_key_not_configured")
    return {
        "provider": provider,
        "protocol": {
            "value": variant["protocol"],
            "source": api_variant["source"],
            "name": api_variant["name"],
        },
        "api_variant": api_variant,
        "model": model,
        "base_url": api_base,
        "auth_mode": auth_mode,
        "api_key": api_key,
        "capabilities": dict(variant["capabilities"]),
    }
