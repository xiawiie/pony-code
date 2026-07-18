"""命令行入口。

这个模块负责把“用户怎么启动 pony”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

from difflib import get_close_matches
import platform
import sys

from pony.config.model import DEFAULT_MODEL
from pony.providers.transport import ProviderTransportError
from pony.security.redaction import redact_artifact, redact_text
from pony.workspace.context import WorkspaceContext

from .arguments import build_arg_parser
from .assembly import build_agent
from .commands import (
    handle_help,
    handle_init,
    handle_session,
)
from .sandbox import handle_sandbox as handle_docker_sandbox
from .diagnostics import (
    handle_config,
    handle_doctor,
    handle_status,
)
from .errors import (
    CLI_EXIT_CONFIG,
    CLI_EXIT_INTERNAL,
    CLI_EXIT_USAGE,
    CliError,
    provider_cli_error,
)
from .memory import handle_memory
from .migration import handle_migrate
from .output import error_envelope, format_json, print_result
from .parser import KNOWN_TOP_LEVEL_COMMANDS, parse_cli_invocation
from .recovery import handle_checkpoints, handle_runs, handle_sessions
from .start import run_agent_once, run_repl


def _handle_recovery_command(cwd, tokens, args):
    """把 `pony --cwd <dir> checkpoints ...` / `runs ...` 分派到 inspection helper。

    这些命令不需要模型 client 也不进入 REPL；它们只对 .pony/checkpoints 和
    .pony/runs 做只读或轻量维护操作。
    """
    if not tokens:
        return None
    head = tokens[0]
    workspace = WorkspaceContext.build(cwd)
    root = workspace.repo_root
    if head == "checkpoints":
        return handle_checkpoints(root, tokens[1:], args)
    if head == "runs":
        return handle_runs(root, tokens[1:], args)
    return None


def _dispatch_help(args, tokens):
    return handle_help(tokens)


def _dispatch_status(args, tokens):
    if tokens:
        raise CliError(
            code="usage",
            message="usage: pony status",
            exit_code=CLI_EXIT_USAGE,
        )
    return handle_status(args.cwd, args)


def _dispatch_doctor(args, tokens):
    return handle_doctor(tokens, args.cwd, args)


def _dispatch_init(args, tokens):
    return handle_init(tokens, args.cwd, args)


def _dispatch_config(args, tokens):
    return handle_config(tokens, args.cwd, args)


def _dispatch_sessions(args, tokens):
    workspace = WorkspaceContext.build(args.cwd)
    return handle_sessions(workspace.repo_root, tokens, args)


def _dispatch_memory(args, tokens):
    workspace = WorkspaceContext.build(args.cwd)
    return handle_memory(tokens, workspace.repo_root, args)


def _dispatch_session(args, tokens):
    workspace = WorkspaceContext.build(args.cwd)
    return handle_session(tokens, workspace.repo_root, args)


def _dispatch_checkpoints(args, tokens):
    return _handle_recovery_command(args.cwd, ["checkpoints", *tokens], args)


def _dispatch_runs(args, tokens):
    return _handle_recovery_command(args.cwd, ["runs", *tokens], args)


def _dispatch_sandbox(args, tokens):
    return handle_docker_sandbox(args, tokens)


def _dispatch_migrate(args, tokens):
    workspace = WorkspaceContext.build(args.cwd)
    payload = handle_migrate(workspace, tokens, args)
    return print_result(
        "migration_status",
        payload,
        args,
        lambda value: "\n".join(f"{key}: {item}" for key, item in value.items()) + "\n",
    )


_PRE_AGENT_COMMAND_HANDLERS = {
    "help": _dispatch_help,
    "init": _dispatch_init,
    "status": _dispatch_status,
    "doctor": _dispatch_doctor,
    "config": _dispatch_config,
    "sessions": _dispatch_sessions,
    "session": _dispatch_session,
    "memory": _dispatch_memory,
    "checkpoints": _dispatch_checkpoints,
    "runs": _dispatch_runs,
    "sandbox": _dispatch_sandbox,
    "migrate": _dispatch_migrate,
}


def _dispatch_pre_agent_command(invocation, args):
    handler = _PRE_AGENT_COMMAND_HANDLERS.get(invocation.command)
    if handler is None:
        return None
    return handler(args, invocation.command_args)


def _validate_agent_command(invocation):
    if getattr(invocation.runtime_args, "mode", None) is not None and (
        invocation.command not in {"run", "repl"}
    ):
        raise CliError(
            code="usage",
            message="--mode is only valid with `pony run` or `pony repl`",
            exit_code=CLI_EXIT_USAGE,
        )
    if getattr(
        invocation.runtime_args, "sandbox", False
    ) and invocation.command not in {
        "run",
        "repl",
    }:
        raise CliError(
            code="usage",
            message="--sandbox is only valid with `pony run` or `pony repl`",
            exit_code=CLI_EXIT_USAGE,
        )
    if getattr(invocation.runtime_args, "sandbox", False) and (
        platform.system() != "Darwin"
        or platform.machine().casefold() not in {"arm64", "aarch64"}
    ):
        raise CliError(
            code="sandbox_local_platform_not_released",
            message="Docker Sandbox local stable is only released for macOS arm64",
            exit_code=CLI_EXIT_CONFIG,
        )
    if invocation.command == "run" and not invocation.command_args:
        raise CliError(
            code="usage",
            message="usage: pony run <prompt...>",
            exit_code=CLI_EXIT_USAGE,
        )
    if invocation.command == "repl" and invocation.command_args:
        raise CliError(
            code="usage",
            message="usage: pony repl",
            exit_code=CLI_EXIT_USAGE,
        )


def _print_cli_error(args, exc):
    safe_details = redact_artifact(exc.details)
    if len(str(safe_details)) > 2000:
        safe_details = {"truncated": True}
    safe_exc = CliError(
        code=redact_text(exc.code)[:300],
        message=redact_text(exc.message)[:300],
        hint=redact_text(exc.hint)[:300],
        exit_code=exc.exit_code,
        details=safe_details,
        category=exc.category,
    )
    if getattr(args, "format", "text") == "json":
        print(format_json(error_envelope(safe_exc)), end="")
    else:
        print(safe_exc.message, file=sys.stderr)
        if safe_exc.category == "provider":
            for label, key in (
                ("code", "code"),
                ("stage", "stage"),
                ("protocol", "protocol"),
                ("reason", "reason"),
                ("http", "http_status"),
            ):
                if key in safe_exc.details:
                    print(f"  {label:<10} {safe_exc.details[key]}", file=sys.stderr)
            if safe_exc.hint:
                print(f"  {'fix':<10} {safe_exc.hint}", file=sys.stderr)
        elif safe_exc.hint:
            print(safe_exc.hint, file=sys.stderr)
    return safe_exc.exit_code


def _print_startup_error(args):
    return _print_cli_error(
        args,
        CliError(
            code="startup_failed",
            message="pony startup failed",
            exit_code=CLI_EXIT_INTERNAL,
        ),
    )


def _raise_on_unknown_command(invocation):
    if invocation.command in KNOWN_TOP_LEVEL_COMMANDS:
        return
    matches = get_close_matches(
        invocation.command,
        sorted(KNOWN_TOP_LEVEL_COMMANDS),
        n=1,
        cutoff=0.8,
    )
    match = matches[0] if matches else ""
    raise CliError(
        code="unknown_command",
        message=f"Unknown command: {invocation.command}",
        hint=f"Did you mean `{match}`?"
        if match
        else "Run `pony help` to see available commands.",
        exit_code=CLI_EXIT_USAGE,
    )


def main(argv=None):
    parser = build_arg_parser()
    invocation = parse_cli_invocation(argv, parser)
    args = invocation.runtime_args
    try:
        _raise_on_unknown_command(invocation)
        _validate_agent_command(invocation)
        # 先分派只读检查命令，避免为它们启动模型 client 或 REPL。
        if invocation.command in _PRE_AGENT_COMMAND_HANDLERS:
            return _dispatch_pre_agent_command(invocation, args)
        agent = build_agent(args)
        if args.mode is not None:
            agent.set_workflow_mode(args.mode)
    except CliError as exc:
        return _print_cli_error(args, exc)
    except ProviderTransportError as exc:
        return _print_cli_error(
            args,
            provider_cli_error(exc, message="Provider verification failed"),
        )
    except ValueError as exc:
        reason = str(exc)
        session_error_code = getattr(exc, "code", "")
        if session_error_code in {
            "session_migration_required",
            "unsupported_legacy_entry",
        }:
            return _print_cli_error(
                args,
                CliError(
                    code=session_error_code,
                    message=reason,
                    exit_code=CLI_EXIT_CONFIG,
                ),
            )
        stable_codes = {
            "api_key_not_configured",
            "api_base_not_configured",
            "api_base_invalid",
            "api_base_credentials",
            "api_base_query_or_fragment",
            "insecure_api_base",
            "model_not_configured",
            "model_invalid",
            "model_session_mismatch",
            "provider_endpoint_conflict",
            "provider_invalid",
        }
        if reason in stable_codes:
            message = {
                "model_session_mismatch": "Provider target does not match this Session",
                "provider_endpoint_conflict": "Provider conflicts with API Base",
                "provider_invalid": "Invalid Provider value",
            }.get(reason, reason)
            return _print_cli_error(
                args,
                CliError(
                    code=reason.replace(" ", "_"),
                    message=message,
                    hint=(
                        "Run `pony init`."
                        if reason
                        in {
                            "api_key_not_configured",
                            "api_base_not_configured",
                            "model_not_configured",
                            "provider_endpoint_conflict",
                            "provider_invalid",
                        }
                        else ""
                    ),
                    exit_code=CLI_EXIT_CONFIG,
                ),
            )
        return _print_cli_error(
            args,
            CliError(
                code="invalid_configuration",
                message="invalid configuration",
                exit_code=CLI_EXIT_CONFIG,
            ),
        )
    except Exception:  # noqa: BLE001 - preserve KeyboardInterrupt/SystemExit
        return _print_startup_error(args)

    try:
        transport = getattr(agent.model_client, "_inner", agent.model_client)
        model = getattr(transport, "model", DEFAULT_MODEL)

        if invocation.command == "repl":
            if args.no_input:
                print(
                    "--no-input cannot be used with interactive repl", file=sys.stderr
                )
                return 2
            return run_repl(
                agent,
                model=model,
                no_color=args.no_color,
                show_header=not args.quiet,
                show_resume=bool(args.resume) and args.format == "text",
            )
        return run_agent_once(agent, invocation.command_args)
    except ProviderTransportError as exc:
        protocol = getattr(
            getattr(agent, "model_client", None),
            "provider_binding",
            {},
        ).get("protocol_family", "")
        return _print_cli_error(
            args,
            provider_cli_error(exc, protocol=protocol),
        )
    except Exception:  # noqa: BLE001 - contain ordinary CLI runtime failures
        return _print_startup_error(args)
