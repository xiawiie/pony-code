import json

from pony.cli.errors import CLI_EXIT_USAGE, CliError
from pony.cli.output import (
    error_envelope,
    format_json,
    success_envelope,
)


def test_success_envelope_has_stable_shape():
    payload = success_envelope("runs_list", [{"run_id": "run_1"}])

    assert payload == {
        "ok": True,
        "kind": "runs_list",
        "data": [{"run_id": "run_1"}],
    }


def test_error_envelope_redacts_to_error_shape():
    error = CliError(
        code="checkpoint_not_found",
        message="Unknown checkpoint: ckpt_missing",
        hint="Run `pony checkpoints list`.",
        exit_code=CLI_EXIT_USAGE,
    )

    payload = error_envelope(error)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "checkpoint_not_found"
    assert payload["error"]["message"] == "Unknown checkpoint: ckpt_missing"
    assert payload["error"]["hint"] == "Run `pony checkpoints list`."


def test_format_json_outputs_parseable_json_with_newline():
    text = format_json(success_envelope("status", {"ok": True}))

    assert text.endswith("\n")
    assert json.loads(text) == {"ok": True, "kind": "status", "data": {"ok": True}}
