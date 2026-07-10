"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import os
from pathlib import Path
import re
import stat
import subprocess
import textwrap
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
from .safe_subprocess import run_hardened_rg
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
        "schema": {"note": "str", "scope": "str='workspace'", "topic": "str=''", "type": "str='feedback'"},
        "risky": False,
        "description": "Append a short note (<=500 chars). With topic → agent/<topic>.md per-topic (Task 21); without topic → agent_notes.md legacy path. Use only when the user explicitly asks to remember.",
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
    "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
    "search": '<tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>',
    "run_shell": f'<tool>{{"name":"run_shell","args":{{"command":"uv run --with pytest python -m pytest -q","timeout":{DEFAULT_RUN_SHELL_TIMEOUT}}}}}</tool>',
    "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
    "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
    "delegate": '<tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>',
    "memory_list": '<tool>{"name":"memory_list","args":{"prefix":"workspace/"}}</tool>',
    "memory_read": '<tool>{"name":"memory_read","args":{"path":"workspace/notes/auth.md","start":1,"end":200}}</tool>',
    "memory_search": '<tool>{"name":"memory_search","args":{"query":"bcrypt","limit":5}}</tool>',
    "memory_save": '<tool>{"name":"memory_save","args":{"note":"bcrypt rounds > 12 causes CI timeout"}}</tool>',
    "repo_lookup": '<tool>{"name":"repo_lookup","args":{"symbol":"AuthMiddleware"}}</tool>',
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
        note = str(args.get("note", "")).strip()
        if not note:
            raise ValueError("note must not be empty")
        if len(note) > MAX_NOTE_CHARS:
            raise ValueError(f"note exceeds {MAX_NOTE_CHARS} chars")
        scope = str(args.get("scope", "workspace"))
        if scope not in ("workspace", "user"):
            raise ValueError("scope must be 'workspace' or 'user'")
        topic = str(args.get("topic", "")).strip()
        if topic and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", topic):
            raise ValueError("invalid topic")
        note_type = str(args.get("type", "feedback")).strip()
        if not note_type:
            raise ValueError("type must not be empty")
        if _contains_sensitive_content(
            context,
            note + "\n" + topic + "\n" + note_type,
        ):
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


def tool_run_shell(context, args):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", DEFAULT_RUN_SHELL_TIMEOUT))
    if timeout < 1 or timeout > MAX_RUN_SHELL_TIMEOUT:
        raise ValueError(f"timeout must be in [1, {MAX_RUN_SHELL_TIMEOUT}]")
    result = subprocess.run(
        command,
        cwd=context.root,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        # 这里传入的是过滤后的环境变量，而不是直接继承整个父 shell 环境，
        # 目的是减少敏感信息被意外带进命令执行环境的风险。
        env=context.shell_env(),
    )
    return textwrap.dedent(
        f"""\
        exit_code: {result.returncode}
        stdout:
        {result.stdout.strip() or "(empty)"}
        stderr:
        {result.stderr.strip() or "(empty)"}
        """
    ).strip()


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
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "memory_list": tool_memory_list,
    "memory_read": tool_memory_read,
    "memory_search": tool_memory_search,
    "memory_save": tool_memory_save,
    "repo_lookup": tool_repo_lookup,
}
