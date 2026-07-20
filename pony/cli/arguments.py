"""Pony command-line argument definitions and validation."""

import argparse
from importlib.metadata import PackageNotFoundError, version

from pony.config.model import validate_model_name
from pony.runtime.application import DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_MAX_STEPS

from .commands import ROOT_HELP


class _RootHelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    pass


def _package_version():
    try:
        return version("pony-code")
    except PackageNotFoundError:
        return "unknown"


def _bounded_int_argument(value, *, name, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(f"{name} must be in [{minimum}, {maximum}]")
    return parsed


def _request_timeout_argument(value):
    return _bounded_int_argument(
        value,
        name="request timeout",
        minimum=1,
        maximum=900,
    )


def _max_steps_argument(value):
    return _bounded_int_argument(
        value,
        name="max steps",
        minimum=1,
        maximum=100,
    )


def _max_output_tokens_argument(value):
    return _bounded_int_argument(
        value,
        name="max output tokens",
        minimum=1,
        maximum=32768,
    )


def _context_window_argument(value):
    return _bounded_int_argument(
        value,
        name="context window",
        minimum=4096,
        maximum=2_000_000,
    )


def _model_argument(value):
    try:
        return validate_model_name(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "model must be a non-empty one-line name"
        ) from exc


def dangerous_bypass_enabled(args):
    return bool(
        getattr(args, "allow_dangerously_skip_permissions", False)
        or getattr(args, "dangerously_skip_permissions", False)
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="pony",
        add_help=False,
        allow_abbrev=False,
        formatter_class=_RootHelpFormatter,
        description="Local coding agent for repository-grounded engineering work.",
        epilog=ROOT_HELP,
    )
    parser.add_argument("-h", "--help", action="store_true", help="help for pony")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    parser.add_argument("prompt", nargs="*", help="Command and arguments.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument(
        "--request-timeout-seconds",
        type=_request_timeout_argument,
        default=300,
        help="Model API request timeout in seconds.",
    )
    parser.add_argument(
        "--resume", default=None, help="Session id to resume or 'latest'."
    )
    parser.add_argument(
        "--model",
        type=_model_argument,
        default=None,
        help="Model for this run/repl Session without changing .env.",
    )
    parser.add_argument(
        "--permission-mode",
        choices=(
            "acceptEdits",
            "auto",
            "bypassPermissions",
            "manual",
            "dontAsk",
            "plan",
        ),
        default=None,
        help="Permission mode for run/repl only.",
    )
    parser.add_argument(
        "--allow-dangerously-skip-permissions",
        action="store_true",
        help="Allow bypassPermissions to be selected for this session.",
    )
    parser.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        help="Bypass permission prompts for this session.",
    )
    parser.add_argument(
        "--allowedTools",
        "--allowed-tools",
        dest="allowed_tool_rules",
        action="append",
        default=[],
        metavar="TOOLS",
        help="Comma or quoted space-separated exact tool names to allow.",
    )
    parser.add_argument(
        "--disallowedTools",
        "--disallowed-tools",
        dest="disallowed_tool_rules",
        action="append",
        default=[],
        metavar="TOOLS",
        help="Comma or quoted space-separated exact tool names to deny.",
    )
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to redact from traces and reports.",
    )
    parser.add_argument(
        "--max-steps",
        type=_max_steps_argument,
        default=DEFAULT_MAX_STEPS,
        help="Maximum tool/model iterations per request.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=_max_output_tokens_argument,
        default=None,
        help=f"Maximum model output tokens per step (default {DEFAULT_MAX_OUTPUT_TOKENS}).",
    )
    parser.add_argument(
        "--context-window",
        type=_context_window_argument,
        default=None,
        help="Model context window override.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format for inspection commands.",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress non-essential output."
    )
    parser.add_argument(
        "--no-color", action="store_true", help="Disable colored output."
    )
    parser.add_argument(
        "--no-input", action="store_true", help="Disable interactive prompts."
    )
    return parser
