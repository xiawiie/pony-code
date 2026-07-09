"""User-facing model connection config."""

from __future__ import annotations

from dataclasses import dataclass, field
import os

from pico.config import ENV_KEY_PATTERN, load_pico_toml_full


DEFAULT_MODEL_NAME = "qwen3.5:4b"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 300


class ModelConnectionConfigError(ValueError):
    """Raised when pico.toml model config is invalid."""


@dataclass(frozen=True)
class ModelConnection:
    name: str
    base_url: str
    api_key_env: str
    api_key: str = field(repr=False)
    api: str | None = None
    timeout: int = DEFAULT_TIMEOUT


def _string_value(raw, default=""):
    if raw is None:
        return default
    if not isinstance(raw, str):
        return default
    return raw.strip()


def _positive_timeout(raw):
    if isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_TIMEOUT
    if raw <= 0:
        return DEFAULT_TIMEOUT
    return raw


def load_model_connection(workspace_root):
    data = load_pico_toml_full(workspace_root)
    model = data.get("model")
    if model is None:
        model = {}
    if not isinstance(model, dict):
        raise ModelConnectionConfigError("[model] in pico.toml must be a table")
    if "api_key" in model:
        raise ModelConnectionConfigError("Store model secrets in an environment variable via api_key_env")

    name = _string_value(model.get("name"), DEFAULT_MODEL_NAME) or DEFAULT_MODEL_NAME
    base_url = (_string_value(model.get("base_url"), DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
    api_key_env = _string_value(model.get("api_key_env"))
    if api_key_env and not ENV_KEY_PATTERN.match(api_key_env):
        raise ModelConnectionConfigError("model api_key_env must be a valid environment variable name")
    api = _string_value(model.get("api")) or None
    timeout = _positive_timeout(model.get("timeout", DEFAULT_TIMEOUT))

    api_key = ""
    if api_key_env:
        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            raise ModelConnectionConfigError(f"Environment variable {api_key_env} is required for model api_key_env")

    return ModelConnection(
        name=name,
        base_url=base_url,
        api_key_env=api_key_env,
        api_key=api_key,
        api=api,
        timeout=timeout,
    )
