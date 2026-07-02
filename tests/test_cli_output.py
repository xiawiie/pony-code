import json

from pico.cli_errors import CLI_EXIT_USAGE, CliError, suggest
from pico.cli_output import (
    error_envelope,
    format_json,
    should_use_color,
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
        hint="Run `pico-cli checkpoints list`.",
        exit_code=CLI_EXIT_USAGE,
    )

    payload = error_envelope(error)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "checkpoint_not_found"
    assert payload["error"]["message"] == "Unknown checkpoint: ckpt_missing"
    assert payload["error"]["hint"] == "Run `pico-cli checkpoints list`."


def test_format_json_outputs_parseable_json_with_newline():
    text = format_json(success_envelope("status", {"ok": True}))

    assert text.endswith("\n")
    assert json.loads(text) == {"ok": True, "kind": "status", "data": {"ok": True}}


def test_should_use_color_respects_cli_and_environment():
    class Tty:
        def isatty(self):
            return True

    assert should_use_color(stream=Tty(), environ={}, no_color=False) is True
    assert should_use_color(stream=Tty(), environ={"NO_COLOR": "1"}, no_color=False) is False
    assert should_use_color(stream=Tty(), environ={"TERM": "dumb"}, no_color=False) is False
    assert should_use_color(stream=Tty(), environ={}, no_color=True) is False


def test_suggest_returns_close_match():
    assert suggest("chekpoints", ["checkpoints", "runs"]) == "checkpoints"
    assert suggest("zzzz", ["checkpoints", "runs"]) == ""
