"""Minimal configured-model live smoke check."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys

from pico.action_codec import ActionCodec
from pico.config import read_project_env
from pico.model_actions import FinalAction
from pico.model_config import load_model_connection
from pico.model_resolver import ModelResolutionError, resolve_model_connection
from pico.providers.factory import build_model_client


def classify_live_error(exc):
    message = str(exc).lower()
    if "http 401" in message or "http 403" in message or "bad key" in message:
        return "auth"
    if "http 429" in message or "rate limit" in message or "too many requests" in message:
        return "rate_limit"
    if "could not reach" in message or "timed out" in message or "connection refused" in message:
        return "network"
    return "code_failure"


def should_fail_all_skipped(results):
    return bool(results) and all(result.get("status") == "skipped" for result in results)


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


def _skip_result(root, exc):
    return {
        "status": "skipped",
        "reason": "config_resolution",
        "error": str(exc),
        "root": str(Path(root).resolve()),
    }


def _ok_result(root, resolved, response, action):
    return {
        "status": "ok",
        "root": str(Path(root).resolve()),
        "model": resolved.name,
        "api": resolved.api,
        "base_url": resolved.base_url,
        "final_text": action.text,
        "usage": dict(getattr(response, "usage", {}) or {}),
    }


def _error_result(root, resolved, exc):
    return {
        "status": "error",
        "root": str(Path(root).resolve()),
        "model": resolved.name,
        "api": resolved.api,
        "base_url": resolved.base_url,
        "error_type": classify_live_error(exc),
        "error": str(exc),
    }


def run_live_model_smoke(root):
    root = Path(root).resolve()
    results = []
    project_env = read_project_env(root, warn=True)

    with _temporary_project_env(project_env):
        try:
            connection = load_model_connection(root)
            resolved = resolve_model_connection(connection)
        except Exception as exc:
            if isinstance(exc, (ValueError, ModelResolutionError)):
                results.append(_skip_result(root, exc))
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
