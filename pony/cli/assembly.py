"""CLI-to-runtime dependency assembly."""

import os
from pathlib import Path

from pony.config.environment import read_project_env
from pony.config.model import resolve_model_config, resolve_session_provider_binding
from pony.config.project import load_pony_toml
from pony.providers.factory import build_transport_client
from pony.providers.probe import resolve_provider_client
from pony.runtime.application import (
    Pony,
    _build_redaction_snapshot,
    _session_requires_bypass_permission_capability,
)
from pony.runtime.options import RuntimeOptions
from pony.runtime.legacy import (
    LegacySandboxResumeError,
    preflight_legacy_sandbox_resume,
)
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
    return _build_agent(args, source_workspace)


def _build_agent(args, source_workspace):
    migration_preflight(source_workspace)
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
    if args.resume and session_id:
        try:
            preflight_legacy_sandbox_resume(source_workspace.repo_root, session_id)
        except LegacySandboxResumeError as exc:
            if exc.code == "legacy_sandbox_session_unsupported":
                raise CliError(
                    code=exc.code,
                    message="Legacy Sandbox sessions cannot resume in Host mode",
                    hint="Inspect the session or start a new Host session.",
                    exit_code=CLI_EXIT_CONFIG,
                ) from exc
            raise CliError(
                code="sandbox_state_invalid",
                message="Legacy Sandbox session binding is invalid",
                details={"reason_code": exc.reason_code},
                exit_code=CLI_EXIT_CONFIG,
            ) from exc
    if store is None and args.resume and session_id and Path(session_store_root).exists():
        store = SessionStore(session_store_root, redactor=redactor)
    if store is not None and args.resume and session_id:
        storage, projection, _tree = store.inspect_readonly(session_id)
        if (
            storage == "current"
            and _session_requires_bypass_permission_capability(projection)
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
    workspace = source_workspace
    if store is None:
        store = SessionStore(session_store_root, redactor=redactor)
    model = _build_transport_client(
        args,
        project_env=project_env,
        process_env=process_env,
        session_store=store if args.resume and session_id else None,
        session_id=session_id if args.resume else None,
    )
    if args.resume and session_id:
        resume_permission_mode = (
            "bypassPermissions"
            if getattr(args, "dangerously_skip_permissions", False)
            else getattr(args, "permission_mode", None)
        )
        return Pony.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            resume_permission_mode=resume_permission_mode,
            resume_permission_rule_updates=getattr(
                args, "_permission_rule_updates", ()
            ),
            options=RuntimeOptions(
                project_trusted=True,
                max_steps=args.max_steps,
                max_output_tokens=max_output_tokens,
                context_window=getattr(args, "context_window", None),
                secret_env_names=configured_secret_names,
                redaction_env=redaction_env,
                trusted_redaction_env=True,
                project_config=project_config,
                allow_dangerously_skip_permissions=dangerous_bypass_enabled(args),
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
            project_config=project_config,
            session_id=session_id,
            allow_dangerously_skip_permissions=dangerous_bypass_enabled(args),
        ),
    )
