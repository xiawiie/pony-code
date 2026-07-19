"""CLI-to-runtime dependency assembly."""

import os
from pathlib import Path

from pony.config.environment import read_project_env
from pony.config.model import resolve_model_config, resolve_session_provider_binding
from pony.config.project import load_pony_toml
from pony.providers.factory import build_transport_client
from pony.providers.probe import resolve_provider_client
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
from pony.security.private_files import private_directory_identity
from pony.security.trust import ProjectTrustStore
from pony.state.session_store import SessionStore
from pony.tools.subprocess import discover_lexical_repo_root
from pony.workspace.context import WorkspaceContext

from .arguments import dangerous_bypass_enabled
from .errors import CLI_EXIT_APPROVAL, CLI_EXIT_CONFIG, CLI_EXIT_USAGE, CliError
from .migration import migration_preflight


def _build_transport_client(
    args,
    *,
    project_env=None,
    process_env=None,
    session_store=None,
    session_id=None,
):
    try:
        config = resolve_model_config(
            project_env=project_env,
            process_env=process_env,
        )
    except ValueError as exc:
        if (
            str(exc) != "api_key_not_configured"
            or session_store is None
            or not session_id
        ):
            raise
        config = resolve_model_config(
            project_env=project_env,
            process_env=process_env,
            required=False,
        )
    if session_store is not None and session_id:
        storage, projection, _tree = session_store.inspect_readonly(session_id)
        if storage == "current":
            config = resolve_session_provider_binding(
                config,
                projection.get("provider_binding", {}),
            )
    if (
        config.get("resolution_status") == "resolved"
        and config.get("auth_mode", {}).get("value") != "none"
        and not config.get("api_key", {}).get("value")
    ):
        raise ValueError("api_key_not_configured")
    client, _resolved, _report = resolve_provider_client(
        config,
        timeout=args.request_timeout_seconds,
        client_builder=build_transport_client,
    )
    return client


def _confirm_project_trust(project_root):
    try:
        answer = input(f"Trust project {project_root}? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _trusted_project_root(args, trust_store, confirm):
    try:
        project_root = discover_lexical_repo_root(Path(args.cwd))
        store = trust_store or ProjectTrustStore(Path.home() / ".pony")
    except (OSError, ValueError) as exc:
        raise CliError(
            code="project_trust_invalid",
            message="Project trust state is invalid",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    if store.is_trusted(project_root):
        identity = private_directory_identity(project_root)
        if store.is_trusted(project_root):
            return project_root, identity, store
    if getattr(args, "no_input", False):
        raise CliError(
            code="project_untrusted",
            message="Project is not trusted",
            exit_code=CLI_EXIT_APPROVAL,
        )
    confirmer = confirm or _confirm_project_trust
    identity = private_directory_identity(project_root)
    if confirmer(project_root) is not True:
        raise CliError(
            code="project_untrusted",
            message="Project is not trusted",
            exit_code=CLI_EXIT_APPROVAL,
        )
    if private_directory_identity(project_root) != identity:
        raise CliError(
            code="project_trust_changed",
            message="Project identity changed during trust confirmation",
            exit_code=CLI_EXIT_CONFIG,
        )
    try:
        store.trust(project_root)
    except (OSError, ValueError) as exc:
        raise CliError(
            code="project_trust_invalid",
            message="Project trust state is invalid",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc
    if not store.is_trusted(project_root):
        raise CliError(
            code="project_trust_changed",
            message="Project identity changed while granting trust",
            exit_code=CLI_EXIT_CONFIG,
        )
    if private_directory_identity(project_root) != identity:
        raise CliError(
            code="project_trust_changed",
            message="Project identity changed while granting trust",
            exit_code=CLI_EXIT_CONFIG,
        )
    return project_root, identity, store


def build_agent(args, *, trust_store=None, confirm=None):
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
    trusted_root, trusted_identity, trust_store = _trusted_project_root(
        args,
        trust_store,
        confirm,
    )
    source_workspace = WorkspaceContext.build(args.cwd)
    if (
        Path(source_workspace.repo_root) != trusted_root
        or private_directory_identity(trusted_root) != trusted_identity
        or not trust_store.is_trusted(trusted_root)
    ):
        raise CliError(
            code="project_trust_changed",
            message="Project identity changed after trust confirmation",
            exit_code=CLI_EXIT_CONFIG,
        )
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
    project_config = load_pony_toml(source_workspace.repo_root)
    session_store_root = source_workspace.repo_root + "/.pony/sessions"
    store = None
    session_id = args.resume
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
    if store is None and args.resume and session_id and Path(session_store_root).exists():
        store = SessionStore(session_store_root, redactor=redactor)
    if store is not None and args.resume and session_id:
        storage, projection, _tree = store.inspect_readonly(session_id)
        if (
            storage == "current"
            and projection.get("permission_mode") == "bypassPermissions"
            and getattr(args, "permission_mode", None) is None
            and not dangerous_bypass_enabled(args)
        ):
            raise CliError(
                code="usage",
                message=(
                    "resuming bypassPermissions requires "
                    "--allow-dangerously-skip-permissions"
                ),
                exit_code=CLI_EXIT_USAGE,
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
    try:
        model = _build_transport_client(
            args,
            project_env=project_env,
            process_env=process_env,
            session_store=store if args.resume and session_id else None,
            session_id=session_id if args.resume else None,
        )
        if args.resume and session_id:
            return Pony.from_session(
                model_client=model,
                workspace=workspace,
                session_store=store,
                session_id=session_id,
                options=RuntimeOptions(
                    project_trusted=True,
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
                project_trusted=True,
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
    except BaseException:
        if sandbox_context is not None:
            _cleanup_failed_sandbox_startup(sandbox_context)
        raise


def _cleanup_failed_sandbox_startup(context):
    store = context.runner.session_store
    try:
        if not context.resumed:
            store.discard(context.sandbox_state_root)
            return
    except (OSError, SandboxSessionError):
        pass
    try:
        session = store.inspect(context.sandbox_state_root)
        lease = session.manifest["lease"]
        if lease is not None:
            store.release(context.sandbox_state_root, lease["owner_nonce"])
    except (OSError, SandboxSessionError):
        pass


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
