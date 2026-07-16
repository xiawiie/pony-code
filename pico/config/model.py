"""Provider specifications and environment-to-Transport resolution."""

import ipaddress
import os
import urllib.parse


PROVIDER_ENV_NAME = "PICO_PROVIDER"
MODEL_ENV_NAME = "PICO_MODEL"
API_URL_ENV_NAME = "PICO_API_URL"
API_KEY_ENV_NAME = "PICO_API_KEY"
API_VARIANT_ENV_NAME = "PICO_API_VARIANT"
AUTH_MODE_ENV_NAME = "PICO_AUTH_MODE"
DEFAULT_PROVIDER = "anthropic"
SUPPORTED_PROVIDERS = ("anthropic", "openai", "ollama")

_PROVIDER_SPECS = {
    "anthropic": {
        "model": "claude-sonnet-4-6",
        "base_url": "https://api.anthropic.com/v1",
        "api_variant": "messages",
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
        "api_variant": "responses",
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
        "api_variant": "chat",
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
DEFAULT_API_URL = _PROVIDER_SPECS[DEFAULT_PROVIDER]["base_url"]
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


def validate_api_url(value):
    raw = str(value or "").strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("api_url_invalid")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("api_url_invalid") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("api_url_credentials")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.casefold().replace("-", "_") in _SECRET_QUERY_KEYS for key, _ in query):
        raise ValueError("api_url_credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("api_url_query_or_fragment")
    if parsed.scheme.casefold() != "https" and not _loopback_api_url(raw):
        raise ValueError("insecure_api_url")
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


def provider_defaults(provider):
    """Return the public defaults for one supported Provider."""
    name = str(provider or "").strip().casefold()
    if name not in _PROVIDER_SPECS:
        raise ValueError("provider_invalid")
    spec = _PROVIDER_SPECS[name]
    return {
        "provider": name,
        "model": spec["model"],
        "base_url": spec["base_url"],
        "api_variant": spec["api_variant"],
        "auth_mode": spec["auth_mode"],
    }


def _normalized_choice(item, *, allowed, error):
    value = str(item["value"] or "").strip().casefold().replace("-", "_")
    if value not in allowed:
        raise ValueError(error)
    return {**item, "value": value}


def resolve_model_config(*, project_env=None, process_env=None, required=True):
    """Resolve Pico's Provider and Transport exclusively from generic variables."""
    project_env = dict(project_env or {})
    process_env = dict(os.environ if process_env is None else process_env)

    provider = _normalized_choice(
        _resolve_required_setting(
            PROVIDER_ENV_NAME,
            project_env,
            process_env,
            required=required,
            default=DEFAULT_PROVIDER,
            default_name="DEFAULT_PROVIDER",
            missing_error="provider_not_configured",
        ),
        allowed=_PROVIDER_SPECS,
        error="provider_invalid",
    )
    spec = _PROVIDER_SPECS[provider["value"]]

    model = _resolve_required_setting(
        MODEL_ENV_NAME,
        project_env,
        process_env,
        required=required,
        default=spec["model"],
        default_name=f"{provider['value']}_default_model",
        missing_error="model_not_configured",
    )
    model["value"] = str(model["value"]).strip()
    if not model["value"]:
        raise ValueError("model_invalid")

    requested_variant = _normalized_choice(
        _resolve_env_value(
            API_VARIANT_ENV_NAME,
            project_env,
            process_env,
            default="auto",
            default_name="provider_default",
        ),
        allowed={"auto", *spec["variants"]},
        error="api_variant_invalid",
    )
    variant_name = (
        spec["api_variant"]
        if requested_variant["value"] == "auto"
        else requested_variant["value"]
    )
    api_variant = {
        **requested_variant,
        "value": variant_name,
        "name": (
            "provider_default"
            if requested_variant["value"] == "auto"
            else API_VARIANT_ENV_NAME
        ),
    }
    variant = spec["variants"][variant_name]

    requested_auth = _normalized_choice(
        _resolve_env_value(
            AUTH_MODE_ENV_NAME,
            project_env,
            process_env,
            default="auto",
            default_name="provider_default",
        ),
        allowed={"auto", "x_api_key", "bearer", "none"},
        error="auth_mode_invalid",
    )
    auth_value = (
        spec["auth_mode"]
        if requested_auth["value"] == "auto"
        else requested_auth["value"].replace("x_api_key", "x-api-key")
    )
    auth_mode = {
        **requested_auth,
        "value": auth_value,
        "name": (
            "provider_default"
            if requested_auth["value"] == "auto"
            else AUTH_MODE_ENV_NAME
        ),
    }

    api_key = _resolve_env_value(API_KEY_ENV_NAME, project_env, process_env)
    key_required = provider["value"] != "ollama" or auth_mode["value"] != "none"
    if required and key_required and not api_key["value"]:
        raise ValueError("api_key_not_configured")
    api_url = _resolve_required_setting(
        API_URL_ENV_NAME,
        project_env,
        process_env,
        required=required,
        default=spec["base_url"],
        default_name=f"{provider['value']}_default_api_url",
        missing_error="api_url_not_configured",
    )
    api_url["value"] = validate_api_url(api_url["value"])
    return {
        "provider": provider,
        "protocol": {
            "value": variant["protocol"],
            "source": api_variant["source"],
            "name": api_variant["name"],
        },
        "api_variant": api_variant,
        "model": model,
        "base_url": api_url,
        "auth_mode": auth_mode,
        "api_key": api_key,
        "capabilities": dict(variant["capabilities"]),
    }
