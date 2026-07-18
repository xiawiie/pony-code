from types import SimpleNamespace

import pytest

from pony.cli.app import build_arg_parser
from pony.cli.parser import KNOWN_TOP_LEVEL_COMMANDS, parse_cli_invocation
from pony.runtime.application import DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_MAX_STEPS


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


def test_parse_workflow_mode_for_run_and_repl():
    run = parse_cli_invocation(
        ["--mode", "plan", "run", "inspect"], build_arg_parser()
    )
    repl = parse_cli_invocation(["repl", "--mode", "review"], build_arg_parser())

    assert run.runtime_args.mode == "plan"
    assert repl.runtime_args.mode == "review"


def test_parse_repl_command():
    invocation = parse_cli_invocation(["repl"], build_arg_parser())

    assert invocation.command == "repl"
    assert invocation.command_args == []


def test_parse_bare_pony_as_interactive_repl():
    invocation = parse_cli_invocation([], build_arg_parser())

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
        (
            ["config", "set-secret", "NAME", "--stdin"],
            ["set-secret", "NAME", "--stdin"],
        ),
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
    assert invocation.command == "repl"


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
    assert args.max_output_tokens is None
    assert args.context_window is None
    assert DEFAULT_MAX_OUTPUT_TOKENS == 16_384
    assert args.request_timeout_seconds == 300
    assert not hasattr(args, "ollama_timeout")
    assert not hasattr(args, "openai_timeout")


def test_parser_rejects_removed_max_new_tokens_flag():
    with pytest.raises(SystemExit) as caught:
        build_arg_parser().parse_args(["--max-new-tokens", "1024"])

    assert caught.value.code == 2


@pytest.mark.parametrize(
    ("flag", "value", "expected"),
    (
        ("--request-timeout-seconds", "1", 1),
        ("--request-timeout-seconds", "900", 900),
        ("--max-steps", "1", 1),
        ("--max-steps", "100", 100),
        ("--max-output-tokens", "1", 1),
        ("--max-output-tokens", "32768", 32768),
        ("--temperature", "0", 0.0),
        ("--temperature", "2", 2.0),
        ("--top-p", "0.0001", 0.0001),
        ("--top-p", "1", 1.0),
    ),
)
def test_runtime_resource_arguments_accept_documented_boundaries(
    flag,
    value,
    expected,
):
    args = build_arg_parser().parse_args([flag, value])

    attribute = flag.removeprefix("--").replace("-", "_")
    assert getattr(args, attribute) == expected


@pytest.mark.parametrize(
    ("flag", "value"),
    (
        ("--request-timeout-seconds", "0"),
        ("--request-timeout-seconds", "901"),
        ("--max-steps", "0"),
        ("--max-steps", "101"),
        ("--max-output-tokens", "0"),
        ("--max-output-tokens", "32769"),
        ("--temperature", "-0.1"),
        ("--temperature", "2.1"),
        ("--temperature", "nan"),
        ("--temperature", "inf"),
        ("--top-p", "0"),
        ("--top-p", "1.1"),
        ("--top-p", "nan"),
        ("--top-p", "inf"),
    ),
)
def test_runtime_resource_arguments_reject_out_of_range_values(flag, value):
    with pytest.raises(SystemExit) as caught:
        build_arg_parser().parse_args([flag, value])

    assert caught.value.code == 2
