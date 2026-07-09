from types import SimpleNamespace

from pico.cli import build_arg_parser
from pico.cli_parser import KNOWN_TOP_LEVEL_COMMANDS, parse_cli_invocation
from pico.runtime import DEFAULT_MAX_NEW_TOKENS, DEFAULT_MAX_STEPS


class RecordingParser:
    def __init__(self):
        self.received_argv = "unset"

    def parse_known_args(self, argv):
        self.received_argv = argv
        return SimpleNamespace(prompt=[]), []


def test_parse_run_command_with_prompt():
    invocation = parse_cli_invocation(
        ["--cwd", "/repo", "run", "fix", "tests"], build_arg_parser()
    )

    assert invocation.command == "run"
    assert invocation.command_args == ["fix", "tests"]
    assert invocation.runtime_args.cwd == "/repo"
    assert invocation.legacy_prompt is False


def test_parse_repl_command():
    invocation = parse_cli_invocation(["repl"], build_arg_parser())

    assert invocation.command == "repl"
    assert invocation.command_args == []


def test_parse_none_preserves_argparse_default_argv_semantics():
    parser = RecordingParser()

    invocation = parse_cli_invocation(None, parser)

    assert parser.received_argv is None
    assert invocation.command == "repl"


def test_parse_legacy_prompt_when_head_is_not_command():
    invocation = parse_cli_invocation(["inspect", "tests"], build_arg_parser())

    assert invocation.command == "run"
    assert invocation.command_args == ["inspect", "tests"]
    assert invocation.legacy_prompt is True


def test_reserved_command_names_are_known():
    assert {
        "run",
        "repl",
        "init",
        "status",
        "doctor",
        "config",
        "runs",
        "sessions",
        "checkpoints",
    }.issubset(KNOWN_TOP_LEVEL_COMMANDS)


def test_parser_defaults_are_generous_for_coding_agent_runs():
    args = build_arg_parser().parse_args([])

    assert args.max_steps == DEFAULT_MAX_STEPS == 12
    assert args.max_new_tokens == DEFAULT_MAX_NEW_TOKENS == 2048
    assert args.model_timeout == 300


def test_parser_no_longer_exposes_provider_axis():
    parser = build_arg_parser()
    option_dests = {action.dest for action in parser._actions}

    assert "provider" not in option_dests
    assert "host" not in option_dests
    assert "base_url" not in option_dests
    assert "ollama_timeout" not in option_dests
    assert "openai_timeout" not in option_dests
