"""Minimal configured-model live smoke check."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys

from pico.action_codec import ActionCodec
from pico.cli_diagnostics import _redact_url_for_diagnostics
from pico.config import read_project_env
from pico.model_actions import FinalAction
from pico.model_config import load_model_connection
from pico.model_resolver import ModelResolutionError, resolve_model_connection
from pico.providers.factory import build_model_client
from pico.security import REDACTED_VALUE, redact_text


_LIVE_ERROR_REDACTION_PATTERNS = (
    (re.compile(r"(?i)authorization\s*:\s*[^\r\n]+"), "Authorization: " + REDACTED_VALUE),
    (re.compile(r"(?i)(bearer\s+)(\S+)"), r"\1" + REDACTED_VALUE),
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{6,}\b"), REDACTED_VALUE),
    (re.compile(r"(?i)\b(api[_-]?key|x-api-key|key|token)\s*=\s*([^\s&]+)"), r"\1=" + REDACTED_VALUE),
)


def classify_live_error(exc):
    message = str(exc).lower()
    if "401" in message and ("http " in message or "http error" in message):
        return "auth"
    if "403" in message and ("http " in message or "http error" in message):
        return "auth"
    if any(
        marker in message
        for marker in (
            "bad key",
            "authentication_error",
            "unauthorized",
            "invalid api key",
            "invalid_api_key",
            "invalid x-api-key",
        )
    ):
        return "auth"
    if "permission denied" in message and any(
        marker in message for marker in ("api key", "x-api-key", "token", "bearer", "credential", "auth")
    ):
        return "auth"
    if "429" in message and ("http " in message or "http error" in message):
        return "rate_limit"
    if "rate limit" in message or "too many requests" in message:
        return "rate_limit"
    if "could not reach" in message or "timed out" in message or "connection refused" in message:
        return "network"
    return "code_failure"


def should_fail_all_skipped(results):
    return bool(results) and all(result.get("status") == "skipped" for result in results)


def _redact_live_error_text(exc, api_key="", base_url=""):
    message = str(exc)
    if base_url:
        message = message.replace(str(base_url), _redact_url_for_diagnostics(base_url))
    env = {"MODEL_API_KEY": api_key} if api_key else {}
    if env:
        message = redact_text(message, env=env, secret_env_names={"MODEL_API_KEY"})
    for pattern, replacement in _LIVE_ERROR_REDACTION_PATTERNS:
        message = pattern.sub(replacement, message)
    return message


@contextmanager
def _temporary_project_env(project_env):
    missing = object()
    previous = {}
    try:
        for name, value in project_env.items():
            previous[name] = os.environ.get(name, missing)
            os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is missing:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _artifact_path(root):
    return Path(root) / "artifacts" / "live-checks" / "live-model-smoke.json"


def _skip_result(root, exc, api_key="", base_url=""):
    return {
        "status": "skipped",
        "reason": "config_resolution",
        "error": _redact_live_error_text(exc, api_key=api_key, base_url=base_url),
        "root": str(Path(root).resolve()),
    }


def _ok_result(root, resolved, response, action):
    return {
        "status": "ok",
        "root": str(Path(root).resolve()),
        "model": resolved.name,
        "api": resolved.api,
        "base_url": _redact_url_for_diagnostics(resolved.base_url),
        "final_text": action.text,
        "usage": dict(getattr(response, "usage", {}) or {}),
    }


def _error_result(root, resolved, exc):
    return {
        "status": "error",
        "root": str(Path(root).resolve()),
        "model": resolved.name,
        "api": resolved.api,
        "base_url": _redact_url_for_diagnostics(resolved.base_url),
        "error_type": classify_live_error(exc),
        "error": _redact_live_error_text(
            exc,
            api_key=getattr(resolved, "api_key", ""),
            base_url=getattr(resolved, "base_url", ""),
        ),
    }


def run_live_model_smoke(root):
    root = Path(root).resolve()
    results = []
    project_env = read_project_env(root, warn=True)

    with _temporary_project_env(project_env):
        connection = None
        try:
            connection = load_model_connection(root)
            resolved = resolve_model_connection(connection)
        except Exception as exc:
            if isinstance(exc, (ValueError, ModelResolutionError)):
                results.append(
                    _skip_result(
                        root,
                        exc,
                        api_key=getattr(connection, "api_key", ""),
                        base_url=getattr(connection, "base_url", ""),
                    )
                )
                return {"results": results}, 2
            raise

        try:
            client = build_model_client(resolved, temperature=0.0, top_p=1.0)
            response = client.complete_v2(
                system=[{"type": "text", "text": "Reply with exactly <final>ok</final> and nothing else."}],
                tools=[],
                messages=[{"role": "user", "content": "Return the requested final tag now."}],
                max_tokens=32,
            )
            action = ActionCodec().decode(response)
            if not isinstance(action, FinalAction) or action.text.strip() != "ok":
                raise RuntimeError(f"expected decoded final text 'ok', got {action!r}")
            results.append(_ok_result(root, resolved, response, action))
        except Exception as exc:
            results.append(_error_result(root, resolved, exc))

    payload = {"results": results}
    if should_fail_all_skipped(results):
        return payload, 2
    if any(result.get("status") == "error" and result.get("error_type") == "code_failure" for result in results):
        return payload, 1
    return payload, 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = argv[0] if argv else "."
    payload, exit_code = run_live_model_smoke(root)
    artifact_path = _artifact_path(root)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    artifact_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
