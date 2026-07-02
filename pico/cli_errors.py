"""Typed CLI errors and exit-code mapping."""

from difflib import get_close_matches


CLI_EXIT_SUCCESS = 0
CLI_EXIT_RUNTIME = 1
CLI_EXIT_USAGE = 2
CLI_EXIT_CONFIG = 3
CLI_EXIT_APPROVAL = 4
CLI_EXIT_INTERNAL = 5


class CliError(Exception):
    def __init__(self, code, message, hint="", exit_code=CLI_EXIT_USAGE, details=None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.hint = str(hint or "")
        self.exit_code = int(exit_code)
        self.details = dict(details or {})


def suggest(value, choices):
    matches = get_close_matches(str(value), [str(choice) for choice in choices], n=1, cutoff=0.6)
    return matches[0] if matches else ""
