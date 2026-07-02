from pico.cli import build_arg_parser
from pico.cli_parser import KNOWN_TOP_LEVEL_COMMANDS, parse_cli_invocation


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


def test_parse_legacy_prompt_when_head_is_not_command():
    invocation = parse_cli_invocation(["inspect", "tests"], build_arg_parser())

    assert invocation.command == "run"
    assert invocation.command_args == ["inspect", "tests"]
    assert invocation.legacy_prompt is True


def test_reserved_command_names_are_known():
    assert {
        "run",
        "repl",
        "status",
        "doctor",
        "config",
        "runs",
        "sessions",
        "checkpoints",
    }.issubset(KNOWN_TOP_LEVEL_COMMANDS)
