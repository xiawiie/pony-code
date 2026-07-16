"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import os
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import stat
import unicodedata
from functools import partial

from . import security as securitylib
from .memory.block_store import MAX_NOTE_CHARS
from .memory.tools import (
    tool_memory_list,
    tool_memory_read,
    tool_memory_save,
    tool_memory_search,
)
from .repo_map import tool_repo_lookup
from .safe_subprocess import run_hardened_command, run_hardened_rg
from .workspace import IGNORED_PATH_NAMES

DEFAULT_RUN_SHELL_TIMEOUT = 60
MAX_RUN_SHELL_TIMEOUT = 120

_RG_SENSITIVE_GLOBS = (
    "!.env",
    "!.env.*",
    "!.envrc",
    "!.netrc",
    "!.npmrc",
    "!.pypirc",
    "!.git-credentials",
    "!credentials.json",
    "!auth.json",
    "!service-account*.json",
    "!secrets.json",
    "!secrets.yaml",
    "!secrets.yml",
    "!secrets.toml",
    "!*.pem",
    "!*.key",
    "!*.p12",
    "!*.pfx",
    "!*.jks",
    "!*.keystore",
    "!**/.ssh/**",
    "!**/.gnupg/**",
    "!**/.aws/credentials",
    "!**/.docker/config.json",
    "!**/.kube/config",
    "!**/.pico/sessions/**",
    "!**/.pico/runs/**",
    "!**/.pico/checkpoints/**",
)
_ALLOWED_ENV_TEMPLATES = frozenset(
    {".env.example", ".env.sample", ".env.template"}
)




_ALLOWED_EFFECT_CLASSES = frozenset({"read_only", "workspace_write", "memory_write"})


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    schema: dict
    description: str
    effect_class: str
    runner: object


class ToolRegistry:
    """工具 schema、描述与 effect class 的唯一注册真源。"""

    def __init__(self):
        self._definitions = {}

    def register(self, name, *, schema, description, effect_class, runner):
        if effect_class not in _ALLOWED_EFFECT_CLASSES or not callable(runner):
            raise ValueError("invalid_tool_definition")
        definition = ToolDefinition(
            name=str(name),
            schema=dict(schema),
            description=str(description),
            effect_class=effect_class,
            runner=runner,
        )
        self._definitions[definition.name] = definition
        return definition

    def require(self, name):
        return self._definitions[str(name)]

    def get(self, name):
        return self._definitions.get(str(name))


def memory_write_intent(current_user, *, history=(), delegated=False):
    """保守识别当前用户输入中的显式持久记忆意图。"""
    del history  # 历史请求不得向当前 turn 继承授权。
    if delegated:
        return False
    text = unicodedata.normalize("NFKC", str(current_user or "")).strip().casefold()
    if not text or re.search(r"\b(?:do not|don't|dont|never)\s+(?:please\s+)?remember\b", text):
        return False
    if re.match(r"^/remember(?:\s|:|$)", text):
        return True
    if re.match(r"^(?:请记住|请保存到记忆|请存入记忆)(?:\s|[:：]|$|[一-鿿])", text):
        return True
    if re.match(r"^(?:remember|please remember)(?:\s|:|$)", text):
        return True
    return bool(re.match(r"^(?:please\s+)?save\b.+\b(?:to|in)\s+(?:the\s+)?memory\b", text))


@dataclass(frozen=True)
class ApprovedShellExecution:
    argv: tuple
    exact_command: str
    execution_mode: str
    executable: object
    cwd: object
    env: dict
    timeout: int


_SANDBOX_PRIVILEGED_EXECUTABLES = frozenset(
    {"sudo", "doas", "pkexec", "open", "osascript", "launchctl"}
)


def sandbox_privilege_denial(
    execution,
    *,
    sandbox_mode,
    allow_git_metadata_writes=False,
):
    if not sandbox_mode:
        return None
    executable_name = Path(str(execution.executable)).name.casefold()
    argv_name = Path(str(execution.argv[0])).name.casefold() if execution.argv else executable_name
    command = str(getattr(execution, "exact_command", "") or "")
    tokens = []
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "sandbox_privilege_denied"
    # Inspect shell segments and wrapper payloads (sh -c, env, command) so an
    # alias cannot turn a broker call into an apparently harmless argv.
    normalized = re.sub(r"(?:&&|\|\||[;|])", " ", command)
    try:
        tokens.extend(shlex.split(normalized, posix=True))
    except ValueError:
        return "sandbox_privilege_denied"
    expanded = [*tokens, *(str(value) for value in execution.argv)]
    for token in tuple(expanded):
        if any(character.isspace() for character in token):
            try:
                expanded.extend(shlex.split(token, posix=True))
            except ValueError:
                return "sandbox_privilege_denied"
    names = {executable_name, argv_name}
    names.update(Path(token).name.casefold() for token in expanded)
    if any(name in _SANDBOX_PRIVILEGED_EXECUTABLES for name in names):
        return "sandbox_privilege_denied"
    if executable_name == "git" and not allow_git_metadata_writes:
        git_subcommands = {"add", "commit", "reset", "checkout", "merge", "rebase", "update-index"}
        if any(str(argument) in git_subcommands for argument in (*execution.argv[1:], *tokens)):
            return "sandbox_git_metadata_write_denied"
    return None


class SensitiveToolError(ValueError):
    """Stable pre-run rejection for sensitive path or content."""

    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


BASE_TOOL_SPECS = {
    "list_files": {
        "schema": {"path": "str='.'"},
        "risky": False,
        "description": "List files in the workspace.",
    },
    "read_file": {
        "schema": {"path": "str", "start": "int=1", "end": "int=200"},
        "risky": False,
        "description": "Read a UTF-8 file by line range.",
    },
    "search": {
        "schema": {"pattern": "str", "path": "str='.'"},
        "risky": False,
        "description": "Search the workspace with rg or a simple fallback.",
    },
    "run_shell": {
        "schema": {"command": "str", "timeout": f"int={DEFAULT_RUN_SHELL_TIMEOUT}"},
        "risky": True,
        "description": "Run a shell command in the repo root.",
    },
    "write_file": {
        "schema": {"path": "str", "content": "str"},
        "risky": True,
        "description": "Write a text file.",
    },
    "patch_file": {
        "schema": {"path": "str", "old_text": "str", "new_text": "str"},
        "risky": True,
        "description": "Replace one exact text block in a file.",
    },
    "memory_list": {
        "schema": {"prefix": "str=''"},
        "risky": False,
        "description": "List memory files (user notes + agent_notes). Optional prefix filter.",
    },
    "memory_read": {
        "schema": {"path": "str", "start": "int=1", "end": "int=200"},
        "risky": False,
        "description": "Read a memory file by line range. Same paging as read_file.",
    },
    "memory_search": {
        "schema": {"query": "str", "limit": "int=5"},
        "risky": False,
        "description": "Full-text search across memory files (BM25 + CJK bigram). Query capped at 512 chars.",
    },
    "memory_save": {
        "schema": {"note": "str", "scope": "str='workspace'"},
        "risky": False,
        "description": "Append a short note (<=500 chars) to agent_notes.md. Use only when the user explicitly asks to remember.",
    },
    "repo_lookup": {
        "schema": {"symbol": "str", "kind": "str=''"},
        "risky": False,
        "description": "Look up where a symbol is defined. Precise for Python (AST), best-effort for TS/Go/Rust (regex).",
    },
}

DELEGATE_TOOL_SPEC = {
    "schema": {"task": "str", "max_steps": "int=3"},
    "risky": False,
    "description": "Ask a bounded read-only child agent to investigate.",
}


def legal_tool_names():
    return set(BASE_TOOL_SPECS) | {"delegate"}

TOOL_EXAMPLES = {
    "list_files": '{"name":"list_files","arguments":{"path":"."}}',
    "read_file": '{"name":"read_file","arguments":{"path":"README.md","start":1,"end":80}}',
    "search": '{"name":"search","arguments":{"pattern":"binary_search","path":"."}}',
    "run_shell": f'{{"name":"run_shell","arguments":{{"command":"uv run --with pytest python -m pytest -q","timeout":{DEFAULT_RUN_SHELL_TIMEOUT}}}}}',
    "write_file": '{"name":"write_file","arguments":{"path":"binary_search.py","content":"def binary_search(nums, target):\\n    return -1\\n"}}',
    "patch_file": '{"name":"patch_file","arguments":{"path":"binary_search.py","old_text":"return -1","new_text":"return mid"}}',
    "delegate": '{"name":"delegate","arguments":{"task":"inspect README.md","max_steps":3}}',
    "memory_list": '{"name":"memory_list","arguments":{"prefix":"workspace/"}}',
    "memory_read": '{"name":"memory_read","arguments":{"path":"workspace/notes/auth.md","start":1,"end":200}}',
    "memory_search": '{"name":"memory_search","arguments":{"query":"bcrypt","limit":5}}',
    "memory_save": '{"name":"memory_save","arguments":{"note":"bcrypt rounds > 12 causes CI timeout"}}',
    "repo_lookup": '{"name":"repo_lookup","arguments":{"symbol":"AuthMiddleware"}}',
}


def build_tool_registry(context):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
        for name, spec in BASE_TOOL_SPECS.items()
    }
    # 子 agent 是刻意做成受限能力的：一旦深度耗尽，
    # 就连 delegate 这个工具都不再暴露给模型。
    if context.depth < context.max_depth:
        tools["delegate"] = {**DELEGATE_TOOL_SPEC, "run": partial(tool_delegate, context)}
    return tools


def tool_example(name):
    return TOOL_EXAMPLES.get(name, "")


def _lexical_tool_target(context, raw_path):
    raw = str(raw_path)
    if not raw or "\x00" in raw:
        raise ValueError("path must not be empty")
    root = Path(context.root).resolve(strict=True)
    source = Path(raw)
    if source.is_absolute() and getattr(
        getattr(context, "sandbox_context", None),
        "workspace_view",
        None,
    ) is not None:
        try:
            candidate = Path(context.path(raw))
        except (OSError, RuntimeError, ValueError):
            raise ValueError("path escapes workspace") from None
    else:
        candidate = Path(
            os.path.abspath(
                os.fspath(source if source.is_absolute() else root / source)
            )
        )
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise ValueError("path escapes workspace") from None
    relative_text = relative.as_posix().casefold()
    if securitylib.is_sensitive_path(relative_text):
        raise SensitiveToolError("sensitive_path_block")

    current = root
    leaf_mode = None
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        except OSError:
            raise ValueError("path access failed") from None
        if stat.S_ISLNK(mode):
            raise ValueError("path escapes workspace: symlink component")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(mode):
            raise ValueError("path parent is not a directory")
        if index == len(relative.parts) - 1:
            leaf_mode = mode
    if (
        securitylib.is_allowed_env_template_leaf(relative_text)
        and leaf_mode is not None
        and not stat.S_ISREG(leaf_mode)
    ):
        raise SensitiveToolError("sensitive_path_block")
    return candidate, relative_text


def _target_mode(path):
    try:
        return path.lstat().st_mode
    except OSError:
        return None


def _contains_sensitive_content(context, value):
    return securitylib.contains_secret_material(
        value,
        env=getattr(context, "redaction_env", None),
        secret_env_names=getattr(context, "secret_env_names", ()),
    )


def validate_tool(context, name, args):
    args = args or {}

    if name == "list_files":
        path, _ = _lexical_tool_target(context, args.get("path", "."))
        mode = _target_mode(path)
        if mode is None or not stat.S_ISDIR(mode):
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path, _ = _lexical_tool_target(context, args["path"])
        mode = _target_mode(path)
        if mode is None or not stat.S_ISREG(mode):
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        _lexical_tool_target(context, args.get("path", "."))
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", DEFAULT_RUN_SHELL_TIMEOUT))
        if timeout < 1 or timeout > MAX_RUN_SHELL_TIMEOUT:
            raise ValueError(f"timeout must be in [1, {MAX_RUN_SHELL_TIMEOUT}]")
        return

    if name == "write_file":
        path, _ = _lexical_tool_target(context, args["path"])
        refusal = _refuse_user_notes_write(context, path)
        if refusal:
            raise ValueError(refusal)
        mode = _target_mode(path)
        if mode is not None and not stat.S_ISREG(mode):
            raise ValueError("path is not a regular file")
        if "content" not in args:
            raise ValueError("missing content")
        if _contains_sensitive_content(context, str(args["content"])):
            raise SensitiveToolError("sensitive_content_block")
        return

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path, _ = _lexical_tool_target(context, args["path"])
        refusal = _refuse_user_notes_write(context, path)
        if refusal:
            raise ValueError(refusal)
        mode = _target_mode(path)
        if mode is None or not stat.S_ISREG(mode):
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        candidate = text.replace(old_text, str(args["new_text"]), 1)
        if _contains_sensitive_content(context, candidate):
            raise SensitiveToolError("sensitive_content_block")
        return

    if name == "delegate":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        if context.depth >= context.max_depth:
            raise ValueError("delegate depth exceeded")
        return

    if name == "memory_list":
        prefix = str(args.get("prefix", "")).strip()
        if len(prefix) > 128:
            raise ValueError("prefix too long")
        if prefix and not re.match(r"^[a-z][a-z0-9/_.-]*$", prefix):
            raise ValueError("invalid prefix format")
        return

    if name == "memory_read":
        path = str(args.get("path", "")).strip()
        if not path:
            raise ValueError("path must not be empty")
        if not re.match(r"^[a-z][a-z0-9/_.-]*$", path):
            raise ValueError("invalid path format")
        if ".." in path.split("/") or path.startswith("/"):
            raise ValueError("path traversal not allowed")
        start = int(args.get("start", 1) or 1)
        end = int(args.get("end", 200) or 200)
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "memory_search":
        query = str(args.get("query", "")).strip()
        if not query:
            raise ValueError("query must not be empty")
        if len(query) > 512:
            raise ValueError("query too long")
        limit = int(args.get("limit", 5) or 5)
        if limit < 1 or limit > 20:
            raise ValueError("limit must be in [1, 20]")
        return

    if name == "memory_save":
        if set(args) - {"note", "scope"}:
            raise ValueError("memory_save accepts only note and scope")
        note = str(args.get("note", "")).strip()
        if not note:
            raise ValueError("note must not be empty")
        if len(note) > MAX_NOTE_CHARS:
            raise ValueError(f"note exceeds {MAX_NOTE_CHARS} chars")
        scope = str(args.get("scope", "workspace"))
        if scope not in ("workspace", "user"):
            raise ValueError("scope must be 'workspace' or 'user'")
        if _contains_sensitive_content(context, note):
            raise SensitiveToolError("sensitive_content_block")
        return

    if name == "repo_lookup":
        symbol = str(args.get("symbol", "")).strip()
        if not symbol:
            raise ValueError("symbol must not be empty")
        if len(symbol) > 128:
            raise ValueError("symbol too long")
        if not re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", symbol):
            raise ValueError("symbol must be a valid identifier")
        kind = str(args.get("kind", ""))
        if kind and kind not in ("class", "function", "method"):
            raise ValueError("kind must be class, function, or method")
        return


def tool_list_files(context, args):
    path = context.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    entries = sorted(path.iterdir(), key=lambda item: item.name.casefold())
    lines = []
    for entry in entries[:200]:
        if entry.name in IGNORED_PATH_NAMES:
            continue
        relative = entry.relative_to(context.root).as_posix()
        if securitylib.is_sensitive_path(relative):
            lines.append(f"{entry.name} [sensitive]")
            continue
        try:
            mode = entry.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISLNK(mode):
            kind = "[L]"
        elif stat.S_ISDIR(mode):
            kind = "[D]"
        elif stat.S_ISREG(mode):
            kind = "[F]"
        else:
            kind = "[?]"
        lines.append(f"{kind} {entry.relative_to(context.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return f"# {path.relative_to(context.root)}\n{body}"


def tool_search(context, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = context.path(args.get("path", "."))

    rg_executable = context.trusted_executables.get("rg")
    if rg_executable:
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        base_rg_args = [
            "-n",
            "--with-filename",
            "--null",
            "--smart-case",
            "--max-count",
            "200",
        ]
        rg_args = list(base_rg_args)
        try:
            target_is_directory = stat.S_ISDIR(path.lstat().st_mode)
        except OSError:
            target_is_directory = False
        if target_is_directory:
            rg_args.append("--glob-case-insensitive")
            for glob in _RG_SENSITIVE_GLOBS:
                rg_args.extend(("--glob", glob))
        rg_args.extend(("-e", pattern, "--", str(path)))
        result = run_hardened_rg(
            rg_executable,
            rg_args,
            cwd=context.root,
        )
        if result.returncode > 1:
            result.check_returncode()
        filtered = (
            _filter_rg_output(context.root, result.stdout)
            if result.returncode == 0
            else "(no matches)"
        )
        matches = [] if filtered == "(no matches)" else filtered.splitlines()

        if target_is_directory and len(matches) < 200:
            templates = _safe_env_template_files(context.root, path)
            if templates:
                template_args = [
                    *base_rg_args,
                    "-e",
                    pattern,
                    "--",
                    *(str(template) for template in templates),
                ]
                template_result = run_hardened_rg(
                    rg_executable,
                    template_args,
                    cwd=context.root,
                )
                if template_result.returncode > 1:
                    template_result.check_returncode()
                if template_result.returncode == 0:
                    template_filtered = _filter_rg_output(
                        context.root,
                        template_result.stdout,
                    )
                    if template_filtered != "(no matches)":
                        template_matches = template_filtered.splitlines()
                        matches.extend(template_matches[: 200 - len(matches)])
        return "\n".join(matches) or "(no matches)"

    matches = []
    try:
        mode = path.lstat().st_mode
    except OSError:
        mode = 0
    files = [path] if stat.S_ISREG(mode) else path.rglob("*") if stat.S_ISDIR(mode) else []
    matches = _python_search_matches(context.root, files, pattern)
    return "\n".join(matches) or "(no matches)"


def _python_search_matches(root, files, pattern, limit=200):
    matches = []
    for file_path in files:
        file_path = _safe_search_file(root, file_path)
        if file_path is None:
            continue
        for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if pattern.lower() in line.lower():
                matches.append(f"{file_path.relative_to(root)}:{number}:{line}")
                if len(matches) >= limit:
                    return matches
    return matches


def _safe_env_template_files(root, directory):
    templates = []
    for candidate in directory.rglob("*"):
        if candidate.name.casefold() not in _ALLOWED_ENV_TEMPLATES:
            continue
        safe_file = _safe_search_file(root, candidate)
        if safe_file is not None:
            templates.append(safe_file)
    templates.sort(key=lambda path: path.as_posix().casefold())
    return templates


def _relative_search_path(root, raw_path):
    raw = str(raw_path)
    if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        return None
    root = Path(root).resolve()
    source = Path(raw)
    candidate = Path(
        os.path.abspath(
            os.fspath(source if source.is_absolute() else root / source)
        )
    )
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    relative_text = relative.as_posix()
    if securitylib.is_sensitive_path(relative_text):
        return None
    return relative_text


def _filter_rg_output(root, output):
    remaining = str(output or "")
    matches = []
    while remaining:
        raw_path, separator, after_path = remaining.partition("\x00")
        if not separator:
            break
        record, newline, remaining = after_path.partition("\n")
        if not newline:
            remaining = ""
        line_number, colon, body = record.partition(":")
        if not colon or not line_number.isdigit():
            continue
        relative = _relative_search_path(root, raw_path)
        if relative is None:
            continue
        matches.append(f"{relative}:{line_number}:{body.rstrip(chr(13))}")
        if len(matches) >= 200:
            break
    return "\n".join(matches) or "(no matches)"


def _safe_search_file(root, candidate):
    relative = _relative_search_path(root, candidate)
    if relative is None:
        return None
    if any(part in IGNORED_PATH_NAMES for part in Path(relative).parts):
        return None
    try:
        return securitylib.require_regular_no_symlink(candidate)
    except (FileNotFoundError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class _ApprovedShellExecution:
    command: str
    argv: tuple[str, ...]
    execution_mode: str
    executable: str
    timeout: int
    sandbox_plan: object = None


def _tool_run_shell(context, execution):
    if not isinstance(execution, _ApprovedShellExecution):
        raise ValueError("run_shell requires an approved execution plan")
    if not Path(execution.executable).is_absolute():
        raise ValueError("trusted executable must be absolute")
    if execution.execution_mode == "argv":
        if not execution.argv:
            raise ValueError("approved argv must not be empty")
        result = run_hardened_command(
            execution.executable,
            args=execution.argv[1:],
            cwd=context.root,
            timeout=execution.timeout,
            env=context.shell_env(),
            return_timeout=True,
        )
    elif execution.execution_mode == "shell":
        result = run_hardened_command(
            execution.executable,
            command=execution.command,
            shell=True,
            cwd=context.root,
            timeout=execution.timeout,
            env=context.shell_env(),
            return_timeout=True,
        )
    else:
        raise ValueError("unsupported approved execution mode")
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.returncode,
        "timed_out": result.timed_out,
        "sandbox_outcome": "timeout" if result.timed_out else "not_applicable",
    }


USER_NOTES_PROTECTED_PREFIX = (".pico", "memory", "notes")


def _refuse_user_notes_write(context, path):
    # User Notes 是用户手写的上下文；`memory_save` 只允许追加到 agent_notes.md。
    # 通用 write_file / patch_file 必须在路径层就拒绝写入 `.pico/memory/notes/**`，
    # 而不是依赖 --approval 拦（`--approval auto` 时不拦）。
    try:
        relative = path.relative_to(context.root)
    except ValueError:
        return ""
    parts = relative.parts
    if len(parts) < len(USER_NOTES_PROTECTED_PREFIX):
        return ""
    if parts[: len(USER_NOTES_PROTECTED_PREFIX)] == USER_NOTES_PROTECTED_PREFIX:
        return f"error: refusing to write user note path (read-only for agent): {relative}"
    return ""


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {path.relative_to(context.root)} ({len(content)} chars)"


def tool_patch_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count != 1:
        raise ValueError(f"old_text must occur exactly once, found {count}")
    path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
    return f"patched {path.relative_to(context.root)}"


def tool_delegate(context, args):
    if context.depth >= context.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")
    return context.spawn_delegate(args)


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search": tool_search,
    "run_shell": _tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "memory_list": tool_memory_list,
    "memory_read": tool_memory_read,
    "memory_search": tool_memory_search,
    "memory_save": tool_memory_save,
    "repo_lookup": tool_repo_lookup,
}
