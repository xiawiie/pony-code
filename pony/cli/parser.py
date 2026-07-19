"""CLI parser helpers for explicit command dispatch."""

from dataclasses import dataclass
import sys


KNOWN_TOP_LEVEL_COMMANDS = {
    "run",
    "repl",
    "init",
    "status",
    "doctor",
    "config",
    "runs",
    "sessions",
    "session",
    "checkpoints",
    "migrate",
    "memory",
    "help",
}


@dataclass
class CliInvocation:
    command: str
    command_args: list
    runtime_args: object


def parse_cli_invocation(argv, parser):
    parse_argv = None if argv is None else list(argv)
    args, extra = parser.parse_known_args(parse_argv)
    tokens = list(args.prompt)
    if extra:
        tokens.extend(extra)
    if getattr(args, "help", False):
        return CliInvocation("help", [], args)
    if not tokens:
        return CliInvocation("repl", [], args)
    head = tokens[0]
    if head in {"run", "repl"}:
        raw_argv = list(sys.argv[1:]) if parse_argv is None else parse_argv
        separator = raw_argv.index("--") if "--" in raw_argv else len(raw_argv)
        _, before_extra = parser.parse_known_args(raw_argv[:separator])
        unknown_options = [
            token
            for token in before_extra
            if token != "-" and token.startswith("-")
        ]
        if unknown_options:
            parser.error(f"unrecognized arguments: {' '.join(unknown_options)}")
        if separator < len(raw_argv) and "--" in extra:
            tokens.remove("--")
    return CliInvocation(head, tokens[1:], args)
