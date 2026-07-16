from types import SimpleNamespace

import pytest

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


def test_parse_repl_command():
    invocation = parse_cli_invocation(["repl"], build_arg_parser())

    assert invocation.command == "repl"
    assert invocation.command_args == []


@pytest.mark.parametrize(
    "argv",
    (
        ["--sandox", "run", "hello"],
        ["run", "hello", "--sandox"],
        ["--sa", "run", "hello"],
        ["run", "hello", "--sa"],
    ),
)
def test_run_rejects_unknown_or_abbreviated_options(argv):
    with pytest.raises(SystemExit) as caught:
        parse_cli_invocation(argv, build_arg_parser())

    assert caught.value.code == 2


def test_run_accepts_option_like_prompt_after_separator():
    invocation = parse_cli_invocation(
        ["--sandbox", "run", "--", "--sandox"],
        build_arg_parser(),
    )

    assert invocation.command == "run"
    assert invocation.command_args == ["--sandox"]
    assert invocation.runtime_args.sandbox is True


@pytest.mark.parametrize(
    ("argv", "command_args"),
    (
        (["doctor", "--check-api"], ["--check-api"]),
        (["sandbox", "prune", "--apply"], ["prune", "--apply"]),
        (["config", "set-secret", "NAME", "--stdin"], ["set-secret", "NAME", "--stdin"]),
    ),
)
def test_subcommand_options_remain_command_arguments(argv, command_args):
    invocation = parse_cli_invocation(argv, build_arg_parser())

    assert invocation.command == argv[0]
    assert invocation.command_args == command_args


def test_parse_none_preserves_argparse_default_argv_semantics():
    parser = RecordingParser()

    invocation = parse_cli_invocation(None, parser)

    assert parser.received_argv is None
    assert invocation.command == "help"


def test_parse_unknown_head_as_unknown_command():
    invocation = parse_cli_invocation(["inspect", "tests"], build_arg_parser())

    assert invocation.command == "inspect"
    assert invocation.command_args == ["tests"]


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
    assert args.request_timeout_seconds == 300
    assert not hasattr(args, "ollama_timeout")
    assert not hasattr(args, "openai_timeout")
