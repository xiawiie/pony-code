"""命令行入口。

这个模块负责把“用户怎么启动 pico”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
from dataclasses import replace
from difflib import get_close_matches
import os
import shutil
import sys

from .cli_commands import (
    ROOT_HELP,
    handle_checkpoints,
    handle_help,
    handle_init,
    handle_memory,
    handle_runs,
    handle_session,
    handle_sessions,
    run_agent_once,
    run_repl,
)
from .cli_diagnostics import handle_config, handle_doctor, handle_status
from .cli_errors import CLI_EXIT_USAGE, CliError
from .cli_help import HELP_DETAILS  # noqa: F401
from .cli_output import error_envelope, format_json
from .cli_parser import parse_cli_invocation
from .config import load_project_env
from .model_config import load_model_connection
from .model_resolver import resolve_model_connection
from .providers.factory import build_model_client as build_resolved_model_client
from .runtime import DEFAULT_MAX_NEW_TOKENS, DEFAULT_MAX_STEPS, Pico, SessionStore
from .workspace import WorkspaceContext, middle


COMMAND_SPECS = {
    "help": {"category": "meta", "subcommands": set()},
    "init": {"category": "config", "subcommands": set()},
    "status": {"category": "inspection", "subcommands": set()},
    "doctor": {"category": "inspection", "subcommands": {"--offline"}},
    "config": {"category": "inspection", "subcommands": {"show"}},
    "sessions": {"category": "inspection", "subcommands": {"list", "show"}},
    "session": {"category": "inspection", "subcommands": {"inspect"}},
    "checkpoints": {"category": "recovery", "subcommands": {"list", "show", "preview-restore", "restore", "prune"}},
    "runs": {"category": "recovery", "subcommands": {"list", "show"}},
}
_RECOVERY_TOP_LEVEL_COMMANDS = {
    name
    for name, spec in COMMAND_SPECS.items()
    if spec["category"] == "recovery"
}
# 只有在第一位是 recovery 顶级命令，且第二位落在下面这些子命令里的时候，
# 才把 argv 当成 recovery inspection 命令。否则用户输入的 `pico "checkpoints ..."`
# 就应该像普通 prompt 一样送进模型。
_RECOVERY_SUBCOMMANDS = {
    name: spec["subcommands"]
    for name, spec in COMMAND_SPECS.items()
    if spec["category"] == "recovery"
}
_COMMAND_NAMESPACE_SUBCOMMANDS = {
    name: spec["subcommands"]
    for name, spec in COMMAND_SPECS.items()
    if spec["subcommands"]
}


class _RootHelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    pass


def _looks_like_recovery_command(prompt_tokens):
    if not prompt_tokens:
        return False
    head = prompt_tokens[0]
    if head not in _RECOVERY_TOP_LEVEL_COMMANDS:
        return False
    # `pico checkpoints` / `pico runs` 单独一个词也算：走默认子命令 list。
    if len(prompt_tokens) == 1:
        return True
    return prompt_tokens[1] in _RECOVERY_SUBCOMMANDS.get(head, set())

DEFAULT_SECRET_ENV_NAMES = (
    "PICO_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "PICO_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "PICO_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "PICO_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

WELCOME_ART = (
    "        /\\___/\\\\",
    "       (  o o  )",
    "       /   ^   \\\\",
    "      /|       |\\\\",
)
WELCOME_NAME = "pico"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "calm shell, ready for work"


SECRET_ENV_NAMES_VAR = "PICO_SECRET_ENV_NAMES"


def _configured_secret_names(args, workspace_root=None):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in getattr(args, "secret_env_names", []))
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    try:
        connection = load_model_connection(workspace_root or getattr(args, "cwd", "."))
        if connection.api_key_env:
            configured_secret_names.add(connection.api_key_env.upper())
    except Exception:
        pass
    return sorted(configured_secret_names)


def _build_model_client(args, workspace_root):
    connection = load_model_connection(workspace_root)
    resolved = resolve_model_connection(connection)
    resolved = replace(resolved, timeout=int(args.model_timeout))
    return build_resolved_model_client(resolved, temperature=args.temperature, top_p=args.top_p)


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
    workspace = WorkspaceContext.build(args.cwd)
    load_project_env(workspace.repo_root)
    configured_secret_names = _configured_secret_names(args, workspace.repo_root)
    store = SessionStore(workspace.repo_root + "/.pico/sessions")
    model = _build_model_client(args, workspace.repo_root)
    session_id = args.resume
    approval_policy = "never" if getattr(args, "no_input", False) and args.approval == "ask" else args.approval
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        return Pico.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=approval_policy,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
        )
    return Pico(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="pico-cli",
        add_help=False,
        allow_abbrev=False,
        formatter_class=_RootHelpFormatter,
        description="Local coding agent for repository-grounded engineering work.",
        epilog=ROOT_HELP,
    )
    parser.add_argument("-h", "--help", action="store_true", help="help for pico-cli")
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--model-timeout", type=int, default=300, help="Model request timeout in seconds.")
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
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format for inspection commands.")
    parser.add_argument("--quiet", action="store_true", help="Suppress non-essential human output.")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output.")
    parser.add_argument("--no-input", action="store_true", help="Disable interactive prompts.")
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
            message="usage: pico-cli status",
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


def _dispatch_recovery(args, tokens, command):
    recovery_tokens = [command, *tokens]
    if not _looks_like_recovery_command(recovery_tokens):
        return None
    return _handle_recovery_command(args.cwd, recovery_tokens, args)


def _dispatch_checkpoints(args, tokens):
    return _dispatch_recovery(args, tokens, "checkpoints")


def _dispatch_runs(args, tokens):
    return _dispatch_recovery(args, tokens, "runs")


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
}


def _dispatch_pre_agent_command(invocation, args):
    handler = _PRE_AGENT_COMMAND_HANDLERS.get(invocation.command)
    if handler is None:
        return None
    return handler(args, invocation.command_args)


def _print_cli_error(args, exc):
    if getattr(args, "format", "text") == "json":
        print(format_json(error_envelope(exc)), end="")
    else:
        print(exc.message, file=sys.stderr)
        if exc.hint:
            print(exc.hint, file=sys.stderr)
    return exc.exit_code


def _raise_on_legacy_command_typo(invocation):
    if not invocation.legacy_prompt or not invocation.command_args:
        return
    head = invocation.command_args[0]
    rest = invocation.command_args[1:]
    if not rest:
        return
    matches = get_close_matches(
        str(head),
        sorted(_COMMAND_NAMESPACE_SUBCOMMANDS),
        n=1,
        cutoff=0.8,
    )
    match = matches[0] if matches else ""
    if match and rest[0] not in _COMMAND_NAMESPACE_SUBCOMMANDS[match]:
        match = ""
    if not match:
        return
    raise CliError(
        code="unknown_command",
        message=f"Unknown command: {head}",
        hint=f"Did you mean `{match}`?",
        exit_code=CLI_EXIT_USAGE,
    )


def main(argv=None):
    parser = build_arg_parser()
    invocation = parse_cli_invocation(argv, parser)
    args = invocation.runtime_args
    try:
        _raise_on_legacy_command_typo(invocation)
        # 先分派只读检查命令，避免为它们启动模型 client 或 REPL。
        pre_agent_result = _dispatch_pre_agent_command(invocation, args)
        if pre_agent_result is not None:
            return pre_agent_result
    except CliError as exc:
        return _print_cli_error(args, exc)
    agent = build_agent(args)

    model = getattr(agent.model_client, "model", "model")
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", ""))
    if not args.quiet:
        print(build_welcome(agent, model=model, host=host))

    if invocation.command == "run":
        return run_agent_once(agent, invocation.command_args)
    if invocation.command == "repl":
        if args.no_input:
            print("--no-input cannot be used with interactive repl", file=sys.stderr)
            return 2
        return run_repl(agent)
    return run_agent_once(agent, [invocation.command, *invocation.command_args])
