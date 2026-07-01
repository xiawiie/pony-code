"""恢复策略：什么文件可以做快照、什么命令允许直接跑。

Phase 1 只需要极简、可解释的启发式。真实的策略配置放在 pico.toml 里，本模块
只负责把“类别”和“默认判决”写清楚。
"""

import shlex
from pathlib import Path

from pico.recovery_paths import (
    normalize_workspace_relative_path,
    resolve_workspace_relative_path,
)


# 单文件快照上限：Phase 1 用固定值 8 MiB。真实用户覆写在 pico.toml 里。
DEFAULT_MAX_BLOB_SIZE = 8 * 1024 * 1024

_BINARY_EXTENSIONS = {
    ".bin",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".class",
    ".jar",
    ".mp3",
    ".mp4",
    ".ico",
    ".wasm",
}

_READ_ONLY_COMMANDS = {
    "ls", "cat", "pwd", "echo", "printf", "head", "tail", "wc", "grep", "rg",
    "find", "stat", "file", "sha256sum", "md5sum", "diff", "which", "type",
    "env", "date", "hostname", "id", "whoami", "tree",
}

_READ_ONLY_GIT_SUBCOMMANDS = {
    "status", "log", "diff", "show", "branch", "rev-parse", "config",
    "remote", "ls-files", "ls-tree", "blame", "shortlog", "describe",
    "reflog", "tag", "worktree",
}

_DESTRUCTIVE_COMMANDS = {"rm", "rmdir", "shred", "trash", "mv", "dd", "shutdown", "reboot"}
_DESTRUCTIVE_GIT_SUBCOMMANDS = {
    "reset", "clean", "checkout", "restore", "push", "rebase", "merge", "commit", "branch", "tag", "gc",
}

_EXTERNAL_EFFECT_COMMANDS = {
    "curl", "wget", "ssh", "scp", "rsync", "docker", "kubectl", "helm",
    "aws", "gcloud", "az", "npm", "pnpm", "yarn", "pip", "uv", "cargo",
    "gh", "git-lfs",
}


def _classify_git(tokens):
    if len(tokens) < 2:
        return "read_only"
    sub = tokens[1].lower()
    if sub in _READ_ONLY_GIT_SUBCOMMANDS and sub not in _DESTRUCTIVE_GIT_SUBCOMMANDS:
        return "read_only"
    if sub in _DESTRUCTIVE_GIT_SUBCOMMANDS:
        return "destructive"
    return "workspace_write"


def command_risk_class(command):
    """把 shell 命令粗分成四个类别：read_only / workspace_write / destructive / external_effect。

    只看命令头（第一个词）和几种常见的子命令。真正的沙箱决策要靠 approval 层。
    """
    if command is None:
        return "workspace_write"
    text = str(command).strip()
    if not text:
        return "workspace_write"
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        tokens = text.split()
    if not tokens:
        return "workspace_write"
    head = Path(tokens[0]).name.lower()

    if head == "git":
        return _classify_git(tokens)
    if head in _DESTRUCTIVE_COMMANDS:
        return "destructive"
    if head in _EXTERNAL_EFFECT_COMMANDS:
        return "external_effect"
    if head in _READ_ONLY_COMMANDS:
        return "read_only"
    return "workspace_write"


def evaluate_command_approval(risk_class):
    """按风险类别给出默认判决。read/write 直接 allow，destructive/external 要人工确认。"""
    if risk_class == "read_only":
        return {"decision": "allow", "reason": "read_only_command"}
    if risk_class == "workspace_write":
        return {"decision": "allow", "reason": "workspace_write_command"}
    if risk_class == "destructive":
        return {"decision": "ask", "reason": "destructive_command"}
    if risk_class == "external_effect":
        return {"decision": "ask", "reason": "external_effect_command"}
    return {"decision": "ask", "reason": "unknown_risk_class"}


def _looks_binary(sample):
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    # 高比例的不可打印字节视作二进制
    textish = sum(1 for byte in sample if byte == 9 or byte == 10 or byte == 13 or 32 <= byte < 127)
    return textish / len(sample) < 0.85


def snapshot_eligibility(workspace_root, raw_path, max_blob_size=DEFAULT_MAX_BLOB_SIZE):
    """判断一个 workspace 相对路径是否适合做快照。

    Phase 1 的门槛：路径合法、文件存在（或不存在→创建场景也算 eligible）、非目录、
    非 symlink、非二进制、字节数不超过 max_blob_size。
    """
    try:
        normalized = normalize_workspace_relative_path(raw_path)
    except ValueError as exc:
        return {"snapshot_eligible": False, "ineligible_reason": "invalid_path", "detail": str(exc), "path": str(raw_path)}
    try:
        resolved = resolve_workspace_relative_path(workspace_root, normalized)
    except ValueError as exc:
        return {"snapshot_eligible": False, "ineligible_reason": "invalid_path", "detail": str(exc), "path": normalized}

    result = {"snapshot_eligible": True, "ineligible_reason": "", "path": normalized}

    if resolved.is_symlink():
        result["snapshot_eligible"] = False
        result["ineligible_reason"] = "symlink"
        return result
    if resolved.exists():
        if resolved.is_dir():
            result["snapshot_eligible"] = False
            result["ineligible_reason"] = "directory"
            return result
        size = resolved.stat().st_size
        if size > max_blob_size:
            result["snapshot_eligible"] = False
            result["ineligible_reason"] = "file_too_large"
            return result
        extension = resolved.suffix.lower()
        if extension in _BINARY_EXTENSIONS:
            result["snapshot_eligible"] = False
            result["ineligible_reason"] = "binary_file"
            return result
        try:
            with open(resolved, "rb") as handle:
                sample = handle.read(4096)
        except OSError as exc:
            result["snapshot_eligible"] = False
            result["ineligible_reason"] = "read_failed"
            result["detail"] = str(exc)
            return result
        if _looks_binary(sample):
            result["snapshot_eligible"] = False
            result["ineligible_reason"] = "binary_file"
            return result
    return result
