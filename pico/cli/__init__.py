"""命令行入口。

这个模块负责把“用户怎么启动 pico”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
from difflib import get_close_matches
from importlib.metadata import PackageNotFoundError, version
import math
import os
from pathlib import Path
import platform
import shutil
import sys

from pico import security as securitylib
from .commands import (
    ROOT_HELP,
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
from .errors import CLI_EXIT_CONFIG, CLI_EXIT_INTERNAL, CLI_EXIT_USAGE, CliError
from .memory import handle_memory
from .migration import handle_migrate, migration_preflight
from .output import error_envelope, format_json, print_result
from .parser import KNOWN_TOP_LEVEL_COMMANDS, parse_cli_invocation
from .recovery import handle_checkpoints, handle_runs, handle_sessions
from .start import run_agent_once, run_repl
from pico.config import (
    DEFAULT_API_URL,
    DEFAULT_MODEL,
    load_pico_toml,
    read_project_env,
    resolve_model_config,
)
from pico.sandbox.docker import (
    build_docker_sandbox_context,
    discover_local_docker,
    DockerSandboxError,
    ensure_runtime_docker_config,
    local_docker_sandbox_runtime,
)
from pico.providers.factory import build_model_client
from pico.runtime import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_STEPS,
    Pico,
    _build_redaction_snapshot,
)
from pico.state.session_store import SessionStore
from pico.sandbox.session import (
    find_project_sandbox_session,
    SandboxSessionError,
    source_mutation_authority,
)
from pico.security import redact_artifact, redact_text
from pico.workspace import WorkspaceContext, middle


class _RootHelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    pass


WELCOME_ART = (
    "        /\\___/\\\\",
    "       (  o o  )",
    "       /   ^   \\\\",
    "      /|       |\\\\",
)
WELCOME_NAME = "pico"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "calm shell, ready for work"


def _package_version():
    try:
        return version("pico")
    except PackageNotFoundError:
        return "unknown"


def _build_model_client(args, *, project_env=None, process_env=None):
    config = resolve_model_config(
        project_env=project_env,
        process_env=process_env,
    )
    return build_model_client(
        config["protocol"]["value"],
        model=config["model"]["value"],
        base_url=config["base_url"]["value"],
        api_key=config["api_key"]["value"],
        timeout=args.request_timeout_seconds,
        auth_mode=config["auth_mode"]["value"],
        capabilities=config["capabilities"],
        compatibility=config["compatibility"],
    )


def build_welcome(agent, model, host):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def _bounded_int_argument(value, *, name, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(
            f"{name} must be in [{minimum}, {maximum}]"
        )
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


def _max_new_tokens_argument(value):
    return _bounded_int_argument(
        value,
        name="max new tokens",
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


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 Pico 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Pico`，或一个从旧 session 恢复出来的 `Pico`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。
    source_workspace = WorkspaceContext.build(args.cwd)
    with source_mutation_authority(
        Path.home() / ".pico" / "sandboxes",
        Path(source_workspace.repo_root),
    ):
        return _build_agent_with_source_authority(args, source_workspace)


def _build_agent_with_source_authority(args, source_workspace):
    migration_preflight(source_workspace)
    sandbox_enabled = getattr(args, "sandbox", False)
    sandbox_product = _load_sandbox_runtime() if sandbox_enabled else None
    sandbox_image, sandbox_authorization = (
        sandbox_product if sandbox_product is not None else (None, None)
    )
    process_env = dict(os.environ)
    project_env = read_project_env(source_workspace.repo_root, warn=True)
    redaction_env, configured_secret_names, redactor = _build_redaction_snapshot(
        source_workspace.repo_root,
        secret_env_names=getattr(args, "secret_env_names", ()),
        process_env=process_env,
        project_env=project_env,
    )
    project_config = (
        load_pico_toml(source_workspace.repo_root) if sandbox_enabled else None
    )
    session_store_root = source_workspace.repo_root + "/.pico/sessions"
    store = None
    session_id = args.resume
    approval_policy = "never" if getattr(args, "no_input", False) and args.approval == "ask" else args.approval
    max_output_tokens = getattr(args, "max_output_tokens", None)
    legacy_max_new_tokens = getattr(args, "legacy_max_new_tokens", None)
    if legacy_max_new_tokens is not None:
        if max_output_tokens is not None:
            raise CliError(
                code="conflicting_model_limits",
                message="Use only --max-output-tokens",
                exit_code=CLI_EXIT_CONFIG,
            )
        print(
            "warning: --max-new-tokens is deprecated; use --max-output-tokens",
            file=sys.stderr,
        )
        max_output_tokens = legacy_max_new_tokens
    if session_id == "latest":
        store = SessionStore(session_store_root, redactor=redactor)
        session_id = store.latest()
    if sandbox_enabled and args.resume and not session_id:
        raise CliError(
            code="sandbox_session_not_found",
            message="sandbox session not found",
            exit_code=CLI_EXIT_CONFIG,
        )
    if sandbox_enabled and not args.resume:
        session_id = Pico.new_session_id()
    if not sandbox_enabled and args.resume and session_id:
        try:
            bound = find_project_sandbox_session(
                Path(source_workspace.repo_root) / ".pico",
                Path(source_workspace.repo_root),
                session_id,
            )
        except SandboxSessionError as exc:
            raise CliError(
                code="sandbox_state_invalid",
                message="Sandbox session binding is invalid",
                details={"reason_code": exc.code},
                exit_code=CLI_EXIT_CONFIG,
            ) from exc
        if bound is not None:
            raise CliError(
                code="sandbox_session_mode_mismatch",
                message="Sandbox session cannot resume in host mode",
                hint="Resume this session with --sandbox.",
                exit_code=CLI_EXIT_CONFIG,
            )
    if sandbox_enabled:
        sandbox_context, workspace = _build_sandbox_context(
            source_workspace,
            sandbox_image,
            authorization=sandbox_authorization,
            pico_session_id=session_id,
            resume=bool(args.resume),
            known_secrets=tuple(
                value.encode("utf-8")
                for _name, value in securitylib.detected_secret_env_items(
                    redaction_env,
                    configured_secret_names,
                )
            ),
        )
    else:
        sandbox_context = None
        workspace = source_workspace
    if store is None:
        store = SessionStore(session_store_root, redactor=redactor)
    model = _build_model_client(
        args,
        project_env=project_env,
        process_env=process_env,
    )
    if args.resume and session_id:
        return Pico.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=approval_policy,
            max_steps=args.max_steps,
            max_output_tokens=max_output_tokens,
            context_window=getattr(args, "context_window", None),
            secret_env_names=configured_secret_names,
            redaction_env=redaction_env,
            _trusted_redaction_env=True,
            sandbox_context=sandbox_context,
            project_config=project_config,
        )
    return Pico(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        max_steps=args.max_steps,
        max_output_tokens=max_output_tokens,
        context_window=getattr(args, "context_window", None),
        secret_env_names=configured_secret_names,
        redaction_env=redaction_env,
        _trusted_redaction_env=True,
        sandbox_context=sandbox_context,
        project_config=project_config,
        session_id=session_id,
    )


def _load_sandbox_runtime():
    try:
        return local_docker_sandbox_runtime()
    except DockerSandboxError as exc:
        raise CliError(
            code=exc.code,
            message="Docker Sandbox local authorization failed",
            details={"reason_code": exc.code},
            exit_code=CLI_EXIT_CONFIG,
        ) from exc


def _build_sandbox_context(
    source_workspace,
    image,
    *,
    authorization,
    pico_session_id,
    resume,
    known_secrets,
):
    try:
        docker_cli, docker_endpoint = discover_local_docker()
        context = build_docker_sandbox_context(
            source_workspace.repo_root,
            authorization=authorization,
            pico_session_id=pico_session_id,
            docker_cli=docker_cli,
            docker_endpoint=docker_endpoint,
            docker_config=ensure_runtime_docker_config(),
            image=image,
            git_executable=source_workspace.trusted_executables.get("git"),
            known_secrets=known_secrets,
            resume=resume,
            source_branch=source_workspace.branch,
            source_status=source_workspace.status,
            source_default_branch=source_workspace.default_branch,
        )
    except (DockerSandboxError, SandboxSessionError, OSError, ValueError) as exc:
        code = getattr(exc, "code", "sandbox_startup_failed")
        raise CliError(
            code=code,
            message="Docker Sandbox startup failed",
            details={"reason_code": code},
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    executables = {
        name: path
        for name, path in source_workspace.trusted_executables.items()
        if name != "git"
    }
    workspace = WorkspaceContext.build(
        context.execution_root,
        executables=executables,
        repo_root_override=context.execution_root,
        inspect_git=False,
        logical_root=context.logical_root,
        branch_override="pico-sandbox",
        default_branch_override="pico-sandbox",
        status_override="sandbox_execution_state_unknown",
    )
    return context, workspace


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
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument("--max-steps", type=_max_steps_argument, default=DEFAULT_MAX_STEPS, help="Maximum tool/model iterations per request.")
    parser.add_argument(
        "--max-output-tokens",
        type=_max_new_tokens_argument,
        default=None,
        help=f"Maximum model output tokens per step (default {DEFAULT_MAX_OUTPUT_TOKENS}).",
    )
    parser.add_argument(
        "--max-new-tokens",
        dest="legacy_max_new_tokens",
        type=_max_new_tokens_argument,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--context-window",
        type=_context_window_argument,
        default=None,
        help="Model context window override.",
    )
    parser.add_argument("--temperature", type=_temperature_argument, default=0.2, help="Ollama sampling temperature.")
    parser.add_argument("--top-p", type=_top_p_argument, default=0.9, help="Ollama top-p sampling value.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format for inspection commands.")
    parser.add_argument("--quiet", action="store_true", help="Suppress non-essential human output.")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output.")
    parser.add_argument("--no-input", action="store_true", help="Disable interactive prompts.")
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Run/repl in local Docker Sandbox (macOS arm64 only).",
    )
    return parser


def _handle_recovery_command(cwd, tokens, args):
    """把 `pico --cwd <dir> checkpoints ...` / `runs ...` 分派到 inspection helper。

    这些命令不需要模型 client 也不进入 REPL；它们只对 .pico/checkpoints 和
    .pico/runs 做只读或轻量维护操作。
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
            message="usage: pico status",
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
    if getattr(invocation.runtime_args, "sandbox", False) and invocation.command not in {
        "run",
        "repl",
    }:
        raise CliError(
            code="usage",
            message="--sandbox is only valid with `pico run` or `pico repl`",
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
            message="usage: pico run <prompt...>",
            exit_code=CLI_EXIT_USAGE,
        )
    if invocation.command == "repl" and invocation.command_args:
        raise CliError(
            code="usage",
            message="usage: pico repl",
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
    )
    if getattr(args, "format", "text") == "json":
        print(format_json(error_envelope(safe_exc)), end="")
    else:
        print(safe_exc.message, file=sys.stderr)
        if safe_exc.hint:
            print(safe_exc.hint, file=sys.stderr)
    return safe_exc.exit_code


def _print_startup_error(args):
    return _print_cli_error(
        args,
        CliError(
            code="startup_failed",
            message="pico startup failed",
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
        hint=f"Did you mean `{match}`?" if match else "Run `pico help` to see available commands.",
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
    except CliError as exc:
        return _print_cli_error(args, exc)
    except ValueError as exc:
        reason = str(exc)
        stable_codes = {
            "api_key_not_configured",
            "api_url_not_configured",
            "api_url_invalid",
            "api_url_credentials",
            "api_url_query_or_fragment",
            "insecure_api_url",
            "provider_invalid",
            "api_variant_invalid",
            "auth_mode_invalid",
            "model_invalid",
            "model_session_mismatch",
        }
        if reason in stable_codes:
            return _print_cli_error(
                args,
                CliError(
                    code=reason.replace(" ", "_"),
                    message=reason,
                    hint=(
                        "Run `pico init`."
                        if reason in {"api_key_not_configured", "api_url_not_configured"}
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
        host = getattr(transport, "base_url", DEFAULT_API_URL)
        if not args.quiet:
            print(build_welcome(agent, model=model, host=host))

        if invocation.command == "repl":
            if args.no_input:
                print("--no-input cannot be used with interactive repl", file=sys.stderr)
                return 2
            return run_repl(agent)
        return run_agent_once(agent, invocation.command_args)
    except Exception:  # noqa: BLE001 - contain ordinary CLI runtime failures
        return _print_startup_error(args)
