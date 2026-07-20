"""CLI output helpers for human and machine output."""

import json


def success_envelope(kind, data):
    return {
        "ok": True,
        "kind": str(kind),
        "data": data,
    }


def error_envelope(error):
    payload = {
        "ok": False,
        "error": {
            "code": error.code,
            "message": error.message,
        },
    }
    if error.hint:
        payload["error"]["hint"] = error.hint
    if error.details:
        payload["error"]["details"] = error.details
    return payload


def format_json(payload):
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def print_result(kind, data, args, text_renderer):
    if getattr(args, "format", "text") == "json":
        print(format_json(success_envelope(kind, data)), end="")
        return 0

    text = text_renderer(data)
    if text and not getattr(args, "quiet", False):
        print(text, end="" if text.endswith("\n") else "\n")
    return 0


def build_inspection_redactor(root, args=None):
    from pony.config.environment import read_project_env
    from pony.runtime.application import _build_redaction_snapshot

    _, _, redactor = _build_redaction_snapshot(
        root,
        secret_env_names=getattr(args, "secret_env_names", ()),
        project_env=read_project_env(
            root,
            warn=False,
            harden=False,
            allow_insecure_mode=True,
        ),
        warn=False,
    )
    return redactor


def print_inspection_result(
    root,
    kind,
    data,
    args,
    text_renderer,
    *,
    redactor=None,
):
    """Render legacy/local inspection data only after read-time sanitizing it."""
    redactor = redactor or build_inspection_redactor(root, args)
    return print_result(kind, redactor(data), args, text_renderer)
