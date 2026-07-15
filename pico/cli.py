"""命令行入口。

这个模块负责把“用户怎么启动 pico”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
from difflib import get_close_matches
from importlib import metadata
import os
from pathlib import Path
import shutil
import sys

from . import sandbox_release_authority as release_authority
from . import security as securitylib
from .cli_commands import (
    ROOT_HELP,
    handle_help,
    handle_init,
    handle_session,
)
from .cli_docker_sandbox import handle_sandbox as handle_docker_sandbox
from .cli_diagnostics import (
    handle_config,
    handle_doctor,
    handle_status,
)
from .cli_errors import CLI_EXIT_CONFIG, CLI_EXIT_INTERNAL, CLI_EXIT_USAGE, CliError
from .cli_memory import handle_memory
from .cli_migration import handle_migrate, migration_preflight
from .cli_output import error_envelope, format_json, print_result
from .cli_parser import KNOWN_TOP_LEVEL_COMMANDS, parse_cli_invocation
from .cli_recovery import handle_checkpoints, handle_runs, handle_sessions
from .cli_start import run_agent_once, run_repl
from .config import (
    load_pico_toml,
    read_project_env,
    resolve_provider_config,
)
from .docker_sandbox import (
    build_docker_sandbox_context,
    default_image_manifest_path,
    discover_local_docker,
    DockerSandboxError,
    ensure_runtime_docker_config,
    local_docker_sandbox_runtime,
    load_image_manifest,
    verify_docker_sandbox_runtime_authorization,
)
from .providers.defaults import (
    DEFAULT_DEEPSEEK_BASE_URL,  # noqa: F401
    DEFAULT_DEEPSEEK_MODEL,  # noqa: F401
    DEFAULT_OLLAMA_HOST,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_PROVIDER,  # noqa: F401
    PROVIDER_CHOICES,
)
from .providers.anthropic_compatible import AnthropicCompatibleModelClient
from .providers.ollama import OllamaModelClient
from .providers.openai_compatible import OpenAICompatibleModelClient
from .providers.text_protocol_adapter import TextProtocolAdapter
from .runtime import (
    DEFAULT_MAX_NEW_TOKENS,
    DEFAULT_MAX_STEPS,
    Pico,
    _build_redaction_snapshot,
)
from .session_store import SessionStore
from .sandbox_session import (
    find_project_sandbox_session,
    SandboxSessionError,
    source_mutation_authority,
)
from .security import redact_artifact, redact_text
from .workspace import WorkspaceContext, middle


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


def _build_model_client(args, *, project_env=None, process_env=None):
    explicit = {
        "provider": getattr(args, "provider", None),
        "model": getattr(args, "model", None),
        "base_url": getattr(args, "base_url", None),
    }
    host = getattr(args, "host", None)
    if host and host != DEFAULT_OLLAMA_HOST:
        explicit["host"] = host
    config = resolve_provider_config(
        explicit=explicit,
        project_env=project_env,
        process_env=process_env,
    )
    provider = config["provider"]["value"]
    model = config["model"]["value"]
    base_url = config["base_url"]["value"]
    api_key = config["api_key"]["value"]
    # CLI 只负责把 provider 选择翻译成具体 client；请求格式、缓存与
    # HTTP 协议差异留在具体 provider 模块和 TextProtocolAdapter 中。
    if provider == "openai":
        return TextProtocolAdapter(OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=None,
            timeout=args.request_timeout_seconds,
        ))
    if provider == "anthropic":
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=None,
            timeout=args.request_timeout_seconds,
        )
    if provider == "deepseek":
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=None,
            timeout=args.request_timeout_seconds,
        )

    return TextProtocolAdapter(OllamaModelClient(
        model=model,
        host=base_url,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.request_timeout_seconds,
    ))


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
            max_new_tokens=args.max_new_tokens,
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
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        redaction_env=redaction_env,
        _trusted_redaction_env=True,
        sandbox_context=sandbox_context,
        project_config=project_config,
        session_id=session_id,
    )


def _sandbox_product_error(reason_code):
    if reason_code in {"sandbox_product_not_enabled", "release_authority_unconfigured"}:
        code = "sandbox_product_not_enabled"
    elif reason_code == "release_attestation_expired":
        code = "sandbox_product_enablement_expired"
    elif reason_code.startswith("sandbox_candidate_attestation"):
        code = reason_code
    else:
        code = "sandbox_product_enablement_invalid"
    return CliError(
        code=code,
        message="Docker Sandbox is not product-enabled",
        hint="Run `pico sandbox prepare` with a product-enabled Pico release.",
        details={"reason_code": reason_code},
        exit_code=CLI_EXIT_CONFIG,
    )


def _load_sandbox_runtime():
    candidate_path = os.environ.get(
        release_authority.CANDIDATE_ATTESTATION_ENV,
        "",
    )
    candidate_nonce = os.environ.get(release_authority.CANDIDATE_NONCE_ENV, "")
    if candidate_path or candidate_nonce:
        if (
            not candidate_path
            or len(candidate_nonce) != 64
            or any(character not in "0123456789abcdef" for character in candidate_nonce)
        ):
            raise _sandbox_product_error("sandbox_candidate_attestation_invalid")
        try:
            envelope = release_authority.read_candidate_attestation(candidate_path)
        except release_authority.ReleaseAuthorityError as exc:
            raise _sandbox_product_error(exc.code) from exc
        attestation_kind = "candidate"
    else:
        try:
            envelope, _payload = release_authority.load_cached_product_envelope()
        except release_authority.ReleaseAuthorityError as exc:
            if exc.code != "sandbox_product_not_enabled":
                raise _sandbox_product_error(exc.code) from exc
            cache_path = (
                release_authority.product_enablement_cache_root()
                / release_authority.PRODUCT_ENABLEMENT_CACHE_NAME
            )
            try:
                cache_path.lstat()
            except FileNotFoundError:
                try:
                    return local_docker_sandbox_runtime()
                except DockerSandboxError as local_error:
                    raise CliError(
                        code=local_error.code,
                        message="Docker Sandbox local authorization failed",
                        details={"reason_code": local_error.code},
                        exit_code=CLI_EXIT_CONFIG,
                    ) from local_error
            except OSError as cache_error:
                raise _sandbox_product_error(
                    "sandbox_product_enablement_invalid"
                ) from cache_error
            raise _sandbox_product_error(
                "sandbox_product_enablement_invalid"
            ) from exc
        attestation_kind = "product"
    try:
        image = load_image_manifest(default_image_manifest_path())
        identity = {
            "package_root": Path(__file__).resolve().parent,
            "distribution_version": metadata.version("pico"),
            "image": image,
        }
        authorization = verify_docker_sandbox_runtime_authorization(
            envelope,
            attestation_kind=attestation_kind,
            candidate_nonce=(candidate_nonce if attestation_kind == "candidate" else ""),
            **identity,
        )
        return image, authorization
    except release_authority.ReleaseAuthorityError as exc:
        raise _sandbox_product_error(exc.code) from exc
    except DockerSandboxError as exc:
        raise CliError(
            code=exc.code,
            message="Docker Sandbox image is not available for this platform",
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
    parser.add_argument("prompt", nargs="*", help="Command and arguments.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument(
        "--provider",
        choices=PROVIDER_CHOICES,
        default=None,
        help="Model backend to use. Defaults to PICO_PROVIDER or deepseek.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to qwen3.5:4b for Ollama, PICO_OPENAI_MODEL for openai, PICO_ANTHROPIC_MODEL for anthropic, and PICO_DEEPSEEK_MODEL for deepseek when set.",
    )
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for deepseek, openai, or anthropic.")
    parser.add_argument(
        "--request-timeout-seconds",
        type=int,
        default=300,
        help="Provider request timeout in seconds.",
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
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS, help="Maximum tool/model iterations per request.")
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Ollama sampling temperature.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Ollama top-p sampling value.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format for inspection commands.")
    parser.add_argument("--quiet", action="store_true", help="Suppress non-essential human output.")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output.")
    parser.add_argument("--no-input", action="store_true", help="Disable interactive prompts.")
    parser.add_argument("--sandbox", action="store_true", help="Run shell tools in the managed sandbox.")
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
        if str(exc) == "unknown provider":
            return _print_cli_error(
                args,
                CliError(
                    code="invalid_provider",
                    message="invalid provider configuration",
                    exit_code=CLI_EXIT_CONFIG,
                ),
            )
        if str(exc) != "provider_base_url_credentials":
            return _print_cli_error(
                args,
                CliError(
                    code="invalid_configuration",
                    message="invalid configuration",
                    exit_code=CLI_EXIT_CONFIG,
                ),
            )
        return _print_cli_error(
            args,
            CliError(
                code="provider_base_url_credentials",
                message="provider_base_url_credentials",
                exit_code=CLI_EXIT_USAGE,
            ),
        )
    except Exception:  # noqa: BLE001 - preserve KeyboardInterrupt/SystemExit
        return _print_startup_error(args)

    try:
        transport = getattr(agent.model_client, "_inner", agent.model_client)
        model = getattr(transport, "model", getattr(args, "model", DEFAULT_OLLAMA_MODEL))
        host = getattr(transport, "host", getattr(transport, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
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
