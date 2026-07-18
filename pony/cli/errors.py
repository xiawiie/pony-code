"""Typed CLI errors and exit-code mapping."""

from difflib import get_close_matches


CLI_EXIT_SUCCESS = 0
CLI_EXIT_RUNTIME = 1
CLI_EXIT_USAGE = 2
CLI_EXIT_CONFIG = 3
CLI_EXIT_APPROVAL = 4
CLI_EXIT_INTERNAL = 5

_PROVIDER_PROTOCOLS = frozenset(
    {
        "anthropic_messages",
        "ollama_chat",
        "openai_chat_completions",
        "openai_responses",
    }
)
_PROVIDER_STAGES = frozenset(
    {"tool_call", "tool_result", "response_decode", "runtime"}
)
_PROVIDER_REASONS = frozenset(
    {
        "reasoning_replay_required",
        "response_shape_invalid",
        "tool_arguments_invalid",
        "tool_call_missing",
        "tool_call_shape_invalid",
        "tool_result_rejected",
    }
)
_PROVIDER_CONFIG_CODES = frozenset(
    {"api_key_not_configured", "invalid_configuration", "missing_credentials"}
)
_PROVIDER_ERROR_CODES = _PROVIDER_CONFIG_CODES | frozenset(
    {
        "backend_error",
        "connection_reset",
        "context_length_exceeded",
        "http_4xx",
        "http_5xx",
        "network_error",
        "provider_detection_failed",
        "provider_protocol_mismatch",
        "rate_limited",
        "redirect_blocked",
        "remote_disconnect",
        "request_timeout",
        "request_too_large",
        "response_too_large",
        "response_truncated",
        "timeout",
        "tls_error",
        "unsupported_stop_reason",
    }
)


class CliError(Exception):
    def __init__(
        self,
        code,
        message,
        hint="",
        exit_code=CLI_EXIT_USAGE,
        details=None,
        category="",
    ):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.hint = str(hint or "")
        self.exit_code = int(exit_code)
        self.details = dict(details or {})
        self.category = str(category or "")


def _safe_provider_details(*, code, stage="", protocol="", reason="", http_status=None):
    details = {"code": str(code)[:100]}
    if stage in _PROVIDER_STAGES:
        details["stage"] = stage
    if protocol in _PROVIDER_PROTOCOLS:
        details["protocol"] = protocol
    if reason in _PROVIDER_REASONS:
        details["reason"] = reason
    if type(http_status) is int and 100 <= http_status <= 599:
        details["http_status"] = http_status
    return details


def _provider_hint(code):
    if code in _PROVIDER_CONFIG_CODES:
        return "Run `pony init`."
    if code in {"provider_detection_failed", "provider_protocol_mismatch"}:
        return "Use a tool-capable endpoint/model or run `pony doctor --check-api`."
    return "Check the configured endpoint and run `pony doctor --check-api`."


def provider_cli_error(error, *, message="Provider request failed", protocol=""):
    raw_code = str(getattr(error, "code", ""))
    code = (
        raw_code
        if raw_code in _PROVIDER_ERROR_CODES
        else "provider_protocol_mismatch"
    )
    details = _safe_provider_details(
        code=code,
        stage=getattr(error, "stage", ""),
        protocol=getattr(error, "protocol_family", "") or protocol,
        reason=getattr(error, "protocol_reason", ""),
        http_status=getattr(error, "http_status", None),
    )
    return CliError(
        code=code,
        message=message,
        hint=_provider_hint(code),
        exit_code=(CLI_EXIT_CONFIG if code in _PROVIDER_CONFIG_CODES else CLI_EXIT_RUNTIME),
        details=details,
        category="provider",
    )


def provider_report_cli_error(report, *, message="Provider verification failed"):
    raw_code = str(report.get("reason_code") or "")
    code = (
        raw_code
        if raw_code in _PROVIDER_ERROR_CODES
        else "provider_detection_failed"
    )
    return CliError(
        code=code,
        message=message,
        hint=_provider_hint(code),
        exit_code=(CLI_EXIT_CONFIG if code in _PROVIDER_CONFIG_CODES else CLI_EXIT_RUNTIME),
        details=_safe_provider_details(
            code=code,
            stage=report.get("stage", ""),
            protocol=report.get("protocol", ""),
            reason=report.get("protocol_reason", ""),
            http_status=report.get("http_status"),
        ),
        category="provider",
    )


def suggest(value, choices):
    matches = get_close_matches(str(value), [str(choice) for choice in choices], n=1, cutoff=0.6)
    return matches[0] if matches else ""
