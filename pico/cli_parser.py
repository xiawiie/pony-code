"""CLI parser helpers for explicit and compatibility command dispatch."""

from dataclasses import dataclass


KNOWN_TOP_LEVEL_COMMANDS = {
    "run",
    "repl",
    "status",
    "doctor",
    "config",
    "runs",
    "sessions",
    "checkpoints",
    "help",
}


@dataclass
class CliInvocation:
    command: str
    command_args: list
    runtime_args: object
    legacy_prompt: bool = False


def parse_cli_invocation(argv, parser):
    argv = list(argv or [])
    args, extra = parser.parse_known_args(argv)
    tokens = list(args.prompt)
    if extra:
        tokens.extend(extra)
    if not tokens:
        return CliInvocation("repl", [], args, legacy_prompt=False)
    head = tokens[0]
    if head in KNOWN_TOP_LEVEL_COMMANDS:
        return CliInvocation(head, tokens[1:], args, legacy_prompt=False)
    return CliInvocation("run", tokens, args, legacy_prompt=True)
