"""CLI output helpers for human and machine output."""

import json
import os
import sys


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


def should_use_color(stream=None, environ=None, no_color=False):
    stream = stream or sys.stdout
    environ = os.environ if environ is None else environ
    if no_color:
        return False
    if environ.get("NO_COLOR") is not None:
        return False
    if environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())
