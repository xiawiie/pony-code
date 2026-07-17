"""CLI-to-runtime dependency assembly."""

import os
from pathlib import Path

from pony.config.environment import read_project_env
from pony.config.model import resolve_model_config
from pony.config.project import load_pony_toml
from pony.providers.factory import build_transport_client
from pony.runtime.application import Pony, _build_redaction_snapshot
from pony.runtime.options import RuntimeOptions
from pony.sandbox.docker import (
    build_docker_sandbox_context,
    discover_local_docker,
    DockerSandboxError,
    ensure_runtime_docker_config,
    local_docker_sandbox_runtime,
)
from pony.sandbox.session import (
    find_project_sandbox_session,
    SandboxSessionError,
    source_mutation_authority,
)
from pony.security import redaction as securitylib
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext

from .errors import CLI_EXIT_CONFIG, CliError
from .migration import migration_preflight


def _build_transport_client(args, *, project_env=None, process_env=None):
    config = resolve_model_config(
        project_env=project_env,
        process_env=process_env,
    )
    return build_transport_client(
        config["protocol"]["value"],
        model=config["model"]["value"],
        base_url=config["base_url"]["value"],
        api_key=config["api_key"]["value"],
        timeout=args.request_timeout_seconds,
        auth_mode=config["auth_mode"]["value"],
        capabilities=config["capabilities"],
    )


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 Pony 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Pony`，或一个从旧 session 恢复出来的 `Pony`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。
    source_workspace = WorkspaceContext.build(args.cwd)
    with source_mutation_authority(
        Path.home() / ".pony" / "sandboxes",
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
        load_pony_toml(source_workspace.repo_root) if sandbox_enabled else None
    )
    session_store_root = source_workspace.repo_root + "/.pony/sessions"
    store = None
    session_id = args.resume
    approval_policy = (
        "never"
        if getattr(args, "no_input", False) and args.approval == "ask"
        else args.approval
    )
    max_output_tokens = getattr(args, "max_output_tokens", None)
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
        session_id = Pony.new_session_id()
    if not sandbox_enabled and args.resume and session_id:
        try:
            bound = find_project_sandbox_session(
                Path(source_workspace.repo_root) / ".pony",
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
            pony_session_id=session_id,
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
    model = _build_transport_client(
        args,
        project_env=project_env,
        process_env=process_env,
    )
    if args.resume and session_id:
        return Pony.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            options=RuntimeOptions(
                approval_policy=approval_policy,
                max_steps=args.max_steps,
                max_output_tokens=max_output_tokens,
                context_window=getattr(args, "context_window", None),
                secret_env_names=configured_secret_names,
                redaction_env=redaction_env,
                trusted_redaction_env=True,
                sandbox_context=sandbox_context,
                project_config=project_config,
            ),
        )
    return Pony(
        model_client=model,
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(
            approval_policy=approval_policy,
            max_steps=args.max_steps,
            max_output_tokens=max_output_tokens,
            context_window=getattr(args, "context_window", None),
            secret_env_names=configured_secret_names,
            redaction_env=redaction_env,
            trusted_redaction_env=True,
            sandbox_context=sandbox_context,
            project_config=project_config,
            session_id=session_id,
        ),
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
    pony_session_id,
    resume,
    known_secrets,
):
    try:
        docker_cli, docker_endpoint = discover_local_docker()
        context = build_docker_sandbox_context(
            source_workspace.repo_root,
            authorization=authorization,
            pony_session_id=pony_session_id,
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
        branch_override="pony-sandbox",
        default_branch_override="pony-sandbox",
        status_override="sandbox_execution_state_unknown",
    )
    return context, workspace
