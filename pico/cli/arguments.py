"""Pico command-line argument definitions and validation."""

import argparse
from importlib.metadata import PackageNotFoundError, version
import math

from pico.runtime.application import DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_MAX_STEPS

from .commands import ROOT_HELP


class _RootHelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    pass


def _package_version():
    try:
        return version("pico")
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


def _bounded_float_argument(
    value,
    *,
    name,
    minimum,
    maximum,
    minimum_exclusive=False,
):
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{name} must be a number") from exc
    lower_ok = parsed > minimum if minimum_exclusive else parsed >= minimum
    if not math.isfinite(parsed) or not lower_ok or parsed > maximum:
        lower = "(" if minimum_exclusive else "["
        raise argparse.ArgumentTypeError(
            f"{name} must be in {lower}{minimum}, {maximum}]"
        )
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


def _temperature_argument(value):
    return _bounded_float_argument(
        value,
        name="temperature",
        minimum=0,
        maximum=2,
    )


def _top_p_argument(value):
    return _bounded_float_argument(
        value,
        name="top-p",
        minimum=0,
        maximum=1,
        minimum_exclusive=True,
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="pico",
        add_help=False,
        allow_abbrev=False,
        formatter_class=_RootHelpFormatter,
        description="Local coding agent for repository-grounded engineering work.",
        epilog=ROOT_HELP,
    )
    parser.add_argument("-h", "--help", action="store_true", help="help for pico")
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
        "--approval",
        choices=("ask", "auto", "never"),
        default="ask",
        help="Approval policy for risky tools.",
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
        "--temperature",
        type=_temperature_argument,
        default=0.2,
        help="Ollama sampling temperature.",
    )
    parser.add_argument(
        "--top-p",
        type=_top_p_argument,
        default=0.9,
        help="Ollama top-p sampling value.",
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
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Run/repl in local Docker Sandbox (macOS arm64 only).",
    )
    return parser
