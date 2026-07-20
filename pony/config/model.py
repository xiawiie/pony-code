"""Provider specifications and environment-to-Transport resolution."""

from copy import deepcopy
import hashlib
import ipaddress
import os
import urllib.parse


PROVIDER_ENV_NAME = "PONY_PROVIDER"
MODEL_ENV_NAME = "PONY_MODEL"
API_BASE_ENV_NAME = "PONY_API_BASE"
API_KEY_ENV_NAME = "PONY_API_KEY"
DEFAULT_PROVIDER = "auto"
DEFAULT_MODEL = ""
DEFAULT_API_BASE = ""
SUPPORTED_PROVIDERS = (
    "auto",
    "openai",
    "openai-chat",
    "openai-responses",
    "anthropic",
    "ollama",
)

_PROTOCOL_SPECS = {
    "anthropic_messages": {
        "provider": "anthropic",
        "family": "anthropic",
        "model": "claude-sonnet-4-6",
        "base_url": "https://api.anthropic.com/v1",
        "api_variant": "messages",
        "auth_mode": "x-api-key",
        "official_capabilities": {
            "prompt_cache": True,
            "strict_tools": True,
            "parallel_tool_control": True,
        },
    },
    "openai_responses": {
        "provider": "openai-responses",
        "family": "openai",
        "model": "gpt-5.4",
        "base_url": "https://api.openai.com/v1",
        "api_variant": "responses",
        "auth_mode": "bearer",
        "official_capabilities": {
            "strict_tools": True,
            "parallel_tool_control": True,
            "reasoning_replay": True,
        },
    },
    "openai_chat_completions": {
        "provider": "openai-chat",
        "family": "openai",
        "model": "gpt-5.4",
        "base_url": "https://api.openai.com/v1",
        "api_variant": "chat_completions",
        "auth_mode": "bearer",
        "official_capabilities": {
            "strict_tools": True,
            "parallel_tool_control": True,
        },
    },
    "ollama_chat": {
        "provider": "ollama",
        "family": "ollama",
        "model": "qwen3:8b",
        "base_url": "http://127.0.0.1:11434",
        "api_variant": "chat",
        "auth_mode": "none",
        "official_capabilities": {},
    },
}
_PROVIDER_PROTOCOLS = {
    "anthropic": ("anthropic_messages",),
    "openai": ("openai_chat_completions", "openai_responses"),
    "openai-chat": ("openai_chat_completions",),
    "openai-responses": ("openai_responses",),
    "ollama": ("ollama_chat",),
}
_PROVIDER_DEFAULT_PROTOCOL = {
    "anthropic": "anthropic_messages",
    "openai": "openai_responses",
    "openai-chat": "openai_chat_completions",
    "openai-responses": "openai_responses",
    "ollama": "ollama_chat",
}
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
    scheme = parsed.scheme.casefold()
    if scheme != "https" and not _loopback_api_url(raw):
        raise ValueError("insecure_api_base")
    host = parsed.hostname.casefold()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port is not None and (scheme, port) not in {("https", 443), ("http", 80)}:
        host = f"{host}:{port}"
    return urllib.parse.urlunsplit((scheme, host, parsed.path.rstrip("/"), "", ""))


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
    return {
        "value": default,
        "source": "default" if default else setting["source"],
        "name": default_name if default else setting["name"],
    }


def _loopback_api_url(value):
    parsed = urllib.parse.urlsplit(str(value))
    host = (parsed.hostname or "").casefold()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _known_protocol(base_url):
    parsed = urllib.parse.urlsplit(base_url)
    host = (parsed.hostname or "").casefold()
    if host == "api.openai.com":
        return "openai_responses"
    if host == "api.anthropic.com":
        return "anthropic_messages"
    if _loopback_api_url(base_url) and parsed.port == 11434:
        return "ollama_chat"
    return ""


def _candidate(protocol, base_url):
    spec = _PROTOCOL_SPECS[protocol]
    capabilities = (
        spec["official_capabilities"] if base_url == spec["base_url"] else {}
    )
    return {
        "provider": spec["provider"],
        "protocol": protocol,
        "api_variant": spec["api_variant"],
        "auth_mode": spec["auth_mode"],
        "capabilities": dict(capabilities),
    }


def _candidate_protocols(provider, base_url):
    known = _known_protocol(base_url)
    if provider == "auto":
        if known:
            return (known,), "known_origin"
        if _loopback_api_url(base_url):
            return (
                "ollama_chat",
                "openai_chat_completions",
                "openai_responses",
            ), ""
        return (
            "openai_chat_completions",
            "openai_responses",
        ), ""
    allowed = _PROVIDER_PROTOCOLS[provider]
    if (
        known
        and _PROTOCOL_SPECS[known]["family"]
        != _PROTOCOL_SPECS[allowed[0]]["family"]
    ):
        raise ValueError("provider_endpoint_conflict")
    if provider == "openai" and not known:
        return allowed, ""
    if provider == "openai":
        return (known,), "known_origin"
    return allowed, "explicit"


def _setting(value="", source="", name=""):
    return {"value": value, "source": source, "name": name}


def validate_model_name(value):
    model = str(value or "")
    if (
        not model
        or model != model.strip()
        or len(model) > 200
        or any(character in model for character in ("\0", "\r", "\n"))
    ):
        raise ValueError("model_invalid")
    return model


def provider_family_for_protocol(protocol):
    spec = _PROTOCOL_SPECS.get(str(protocol))
    return str(spec.get("family", "")) if isinstance(spec, dict) else ""


def _default_protocol(provider):
    return _PROVIDER_DEFAULT_PROTOCOL.get(provider, "")


def _config_default(provider, field):
    protocol = _default_protocol(provider)
    return _PROTOCOL_SPECS[protocol][field] if protocol else ""


def resolve_model_config(*, project_env=None, process_env=None, required=True):
    """Resolve four environment values without network access."""
    project_env = dict(project_env or {})
    process_env = dict(os.environ if process_env is None else process_env)

    provider = _resolve_env_value(PROVIDER_ENV_NAME, project_env, process_env)
    provider_value = str(provider["value"] or "").strip().casefold() or "auto"
    provider["value"] = provider_value
    if provider["source"] == "unset":
        provider.update(source="default", name="DEFAULT_PROVIDER")
    if provider_value not in SUPPORTED_PROVIDERS:
        raise ValueError("provider_invalid")

    api_base = _resolve_required_setting(
        API_BASE_ENV_NAME,
        project_env,
        process_env,
        required=required,
        default=_config_default(provider_value, "base_url"),
        default_name=f"{provider_value}_default_api_base",
        missing_error="api_base_not_configured",
    )
    if api_base["value"]:
        api_base["value"] = validate_api_base(api_base["value"])

    candidates = []
    resolution_source = ""
    if api_base["value"]:
        protocols, resolution_source = _candidate_protocols(
            provider_value,
            api_base["value"],
        )
        candidates = [_candidate(protocol, api_base["value"]) for protocol in protocols]

    model_default = _config_default(provider_value, "model")
    if not model_default and len(candidates) == 1:
        model_default = _PROTOCOL_SPECS[candidates[0]["protocol"]]["model"]
    model = _resolve_required_setting(
        MODEL_ENV_NAME,
        project_env,
        process_env,
        required=required,
        default=model_default,
        default_name=(
            f"{candidates[0]['provider']}_default_model"
            if model_default and len(candidates) == 1
            else f"{provider_value}_default_model"
        ),
        missing_error="model_not_configured",
    )
    model_value = str(model["value"] or "")
    model["value"] = validate_model_name(model_value) if model_value else ""

    configuration_error = ""
    if not api_base["value"]:
        configuration_error = "api_base_not_configured"
    elif not model["value"]:
        configuration_error = "model_not_configured"
    resolution_status = (
        "invalid"
        if configuration_error
        else "resolved"
        if len(candidates) == 1
        else "probe_required"
    )
    selected = candidates[0] if resolution_status == "resolved" else None
    auth_mode = selected["auth_mode"] if selected else (
        "bearer" if provider_value == "openai" else ""
    )
    api_key = _resolve_env_value(API_KEY_ENV_NAME, project_env, process_env)
    possible_auth_modes = {candidate["auth_mode"] for candidate in candidates}
    key_required = auth_mode not in {"", "none"} or (
        bool(possible_auth_modes) and "none" not in possible_auth_modes
    )
    if required and key_required and not api_key["value"]:
        raise ValueError("api_key_not_configured")

    resolved_source = resolution_source if selected else ""
    setting_source = (
        api_base if resolved_source == "known_origin" else provider
    )
    return {
        "provider": provider,
        "resolved_provider": _setting(
            selected["provider"] if selected else "",
            resolved_source,
            provider["name"] if selected else "",
        ),
        "resolution_status": resolution_status,
        "resolution_source": resolved_source,
        "resolution_error": configuration_error,
        "candidates": deepcopy(candidates) if resolution_status == "probe_required" else [],
        "protocol": _setting(
            selected["protocol"] if selected else "",
            setting_source["source"] if selected else "",
            setting_source["name"] if selected else "",
        ),
        "api_variant": _setting(
            selected["api_variant"] if selected else "",
            setting_source["source"] if selected else "",
            setting_source["name"] if selected else "",
        ),
        "model": model,
        "base_url": api_base,
        "auth_mode": _setting(
            auth_mode,
            (setting_source if selected else provider)["source"] if auth_mode else "",
            (setting_source if selected else provider)["name"] if auth_mode else "",
        ),
        "api_key": api_key,
        "capabilities": dict(selected["capabilities"]) if selected else {},
    }


def resolve_provider_candidate(config, protocol_family):
    """Project one advertised probe candidate into a resolved model config."""
    selected = next(
        (
            candidate
            for candidate in config.get("candidates", [])
            if candidate.get("protocol") == protocol_family
        ),
        None,
    )
    if selected is None:
        raise ValueError("provider_detection_failed")
    resolved = deepcopy(config)
    resolved["resolved_provider"] = _setting(selected["provider"], "probe", "")
    resolved["resolution_status"] = "resolved"
    resolved["resolution_source"] = "probe"
    resolved["resolution_error"] = ""
    resolved["candidates"] = []
    for key in ("protocol", "api_variant", "auth_mode"):
        resolved[key] = _setting(selected[key], "probe", "")
    resolved["capabilities"] = dict(selected["capabilities"])
    return resolved


def resolve_session_provider_binding(config, binding):
    """Reuse a compatible Session target without probing the network."""
    base_url = config.get("base_url", {}).get("value", "")
    protocol = binding.get("protocol_family") if isinstance(binding, dict) else None
    expected_hash = "sha256:" + hashlib.sha256(str(base_url).encode("utf-8")).hexdigest()
    try:
        session_model = validate_model_name(binding.get("model"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("model_session_mismatch") from exc
    if (
        config.get("resolution_status") == "invalid"
        or not isinstance(protocol, str)
        or protocol not in _PROTOCOL_SPECS
        or binding.keys() != {"protocol_family", "model", "endpoint_hash"}
        or binding.get("endpoint_hash") != expected_hash
    ):
        raise ValueError("model_session_mismatch")

    provider = config.get("provider", {}).get("value")
    try:
        allowed, _source = _candidate_protocols(provider, base_url)
    except (KeyError, ValueError) as exc:
        raise ValueError("model_session_mismatch") from exc
    resolved_protocol = config.get("protocol", {}).get("value", "")
    if protocol not in allowed or (
        config.get("resolution_status") == "resolved"
        and protocol != resolved_protocol
    ):
        raise ValueError("model_session_mismatch")

    selected = _candidate(protocol, base_url)
    resolved = deepcopy(config)
    resolved["resolved_provider"] = _setting(selected["provider"], "session_binding", "")
    resolved["resolution_status"] = "resolved"
    resolved["resolution_source"] = "session_binding"
    resolved["resolution_error"] = ""
    resolved["candidates"] = []
    resolved["model"] = _setting(session_model, "session_binding", "")
    for key in ("protocol", "api_variant", "auth_mode"):
        resolved[key] = _setting(selected[key], "session_binding", "")
    resolved["capabilities"] = dict(selected["capabilities"])
    return resolved
