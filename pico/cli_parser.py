"""CLI parser helpers for explicit command dispatch."""

from dataclasses import dataclass


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
        return CliInvocation("help", [], args)
    head = tokens[0]
    return CliInvocation(head, tokens[1:], args)
