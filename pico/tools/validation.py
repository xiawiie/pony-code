"""Tool argument, workspace-path, and content validation."""

import os
from pathlib import Path
import re
import stat

from pico.memory.block_store import MAX_NOTE_STORAGE_BYTES
from pico.security import private_files as private_files
from pico.security import paths as security_paths
from pico.security import workspace_files as workspace_files
from pico.security import redaction as redaction

from .shell import DEFAULT_RUN_SHELL_TIMEOUT, MAX_RUN_SHELL_TIMEOUT


MAX_WORKSPACE_FILE_BYTES = 8 * 1024 * 1024

MAX_WORKSPACE_DIRECTORY_ENTRIES = 10_000

MAX_WORKSPACE_LIST_RESULTS = 200

MAX_WORKSPACE_SEARCH_DEPTH = 32

MAX_WORKSPACE_SEARCH_FILES = 10_000

MAX_WORKSPACE_SEARCH_BYTES = 64 * 1024 * 1024

MAX_WORKSPACE_SEARCH_MATCHES = 200

USER_NOTES_PROTECTED_PREFIX = (".pico", "memory", "notes")


class SensitiveToolError(ValueError):
    """Stable pre-run rejection for sensitive path or content."""

    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


def _lexical_tool_target(context, raw_path):
    raw = str(raw_path)
    if not raw or "\x00" in raw:
        raise ValueError("path must not be empty")
    root = Path(context.root).resolve(strict=True)
    expected_root_identity = getattr(context, "workspace_root_identity", None)
    if expected_root_identity is not None and private_files.private_directory_identity(
        root
    ) != tuple(expected_root_identity):
        raise workspace_files.WorkspaceIOError(
            "workspace_entry_unsafe",
            "workspace root changed",
        )
    source = Path(raw)
    if (
        source.is_absolute()
        and getattr(
            getattr(context, "sandbox_context", None),
            "workspace_view",
            None,
        )
        is not None
    ):
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
    if security_paths.is_sensitive_path(relative_text):
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
            raise workspace_files.WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace path access failed",
            ) from None
        if stat.S_ISLNK(mode):
            raise workspace_files.WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace path has a symlink component",
            )
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(mode):
            raise workspace_files.WorkspaceIOError(
                "workspace_entry_unsafe",
                "workspace parent is not a directory",
            )
        if index == len(relative.parts) - 1:
            leaf_mode = mode
    if (
        security_paths.is_allowed_env_template_leaf(relative_text)
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


def _target_stat(path):
    try:
        return path.lstat()
    except OSError:
        return None


def _workspace_root_identity(context):
    identity = getattr(context, "workspace_root_identity", None)
    if identity is not None:
        return tuple(identity)
    return private_files.private_directory_identity(context.root)


def _anchored_tool_relative(context, raw_path, *, allow_root=False):
    path, _relative_text = _lexical_tool_target(context, raw_path)
    root = Path(os.path.abspath(os.fspath(context.root)))
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("path escapes workspace") from None
    if not relative.parts:
        if allow_root:
            return "."
        raise ValueError("path must name a workspace entry")
    return relative.as_posix()


def _read_workspace_file(context, raw_path):
    relative = _anchored_tool_relative(context, raw_path)
    state = workspace_files.read_regular_bytes_anchored(
        context.root,
        relative,
        max_bytes=MAX_WORKSPACE_FILE_BYTES,
        expected_root_identity=_workspace_root_identity(context),
    )
    if not state["exists"]:
        raise ValueError("path is not a file")
    return relative, state


def _decode_workspace_utf8(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise workspace_files.WorkspaceIOError(
            "workspace_entry_unsafe",
            "workspace file is not valid UTF-8",
        ) from None


def _encode_workspace_utf8(value):
    try:
        data = str(value).encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("content must be valid UTF-8") from None
    if len(data) > MAX_WORKSPACE_FILE_BYTES:
        raise workspace_files.WorkspaceIOError(
            "workspace_file_limit_exceeded",
            "workspace file exceeds the configured limit",
        )
    return data


def _contains_sensitive_content(context, value):
    return redaction.contains_secret_material(
        value,
        env=getattr(context, "redaction_env", None),
        secret_env_names=getattr(context, "secret_env_names", ()),
    )


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
        return (
            f"error: refusing to write user note path (read-only for agent): {relative}"
        )
    return ""


def validate_tool(context, name, args):
    args = args or {}

    if name == "list_files":
        path, _ = _lexical_tool_target(context, args.get("path", "."))
        mode = _target_mode(path)
        if mode is None:
            raise ValueError("path is not a directory")
        if not stat.S_ISDIR(mode):
            raise workspace_files.WorkspaceIOError(
                "workspace_entry_unsafe",
                "path is not a directory",
            )
        return

    if name == "read_file":
        path, _ = _lexical_tool_target(context, args["path"])
        info = _target_stat(path)
        if info is None:
            raise ValueError("path is not a file")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise workspace_files.WorkspaceIOError(
                "workspace_entry_unsafe",
                "path is not a regular file",
            )
        if info.st_size > MAX_WORKSPACE_FILE_BYTES:
            raise workspace_files.WorkspaceIOError(
                "workspace_file_limit_exceeded",
                "workspace file exceeds the configured limit",
            )
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        if end - start + 1 > 200:
            raise ValueError("read_file accepts at most 200 lines")
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
        info = _target_stat(path)
        if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise workspace_files.WorkspaceIOError(
                "workspace_entry_unsafe",
                "path is not a regular file",
            )
        if "content" not in args:
            raise ValueError("missing content")
        _encode_workspace_utf8(args["content"])
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
        info = _target_stat(path)
        if info is None:
            raise ValueError("path is not a file")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise workspace_files.WorkspaceIOError(
                "workspace_entry_unsafe",
                "path is not a regular file",
            )
        if info.st_size > MAX_WORKSPACE_FILE_BYTES:
            raise workspace_files.WorkspaceIOError(
                "workspace_file_limit_exceeded",
                "workspace file exceeds the configured limit",
            )
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        _relative, state = _read_workspace_file(context, args["path"])
        text = _decode_workspace_utf8(state["data"])
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        candidate = text.replace(old_text, str(args["new_text"]), 1)
        _encode_workspace_utf8(candidate)
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
        if len(note.encode("utf-8")) > MAX_NOTE_STORAGE_BYTES:
            raise ValueError(f"note exceeds {MAX_NOTE_STORAGE_BYTES} bytes")
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
