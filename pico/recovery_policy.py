"""恢复策略：什么文件可以做快照、什么命令允许直接跑。

Phase 1 只需要极简、可解释的启发式。真实的策略配置放在 pico.toml 里，本模块
只负责把“类别”和“默认判决”写清楚。
"""

import re
import shlex
from pathlib import Path

from pico.recovery_paths import (
    normalize_workspace_relative_path,
    resolve_workspace_relative_path,
)

# 组合运算符：任何一种都能把“看似安全的第一段”后面拼上任意命令。
# 早先版本只识别 > >> | && ;，漏掉 || & 后台运行、输入重定向、命令替换等。
_COMPOSITE_OPERATORS = {">", ">>", "<", "<<", "|", "||", "&&", "&", ";"}

# 命令替换/子 shell 也算复合结构；出现即触发递归分类。
_SUBSHELL_INTRO_TOKENS = ("$(", "`", "(")

# shell wrapper 递归的硬上限；深度到这个数还没有底就直接按最保守 destructive 兜底，
# 避免恶意/失控输入把 Python 递归栈炸掉。
_MAX_SHELL_WRAPPER_DEPTH = 32


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

# sh/bash/zsh 之类的 shell wrapper 会用 -c 参数把真正的命令藏在字符串里，
# 单看第一个 token 会漏判。任何 shell wrapper 都必须递归解析 -c 后面的内容。
_SHELL_WRAPPERS = {"sh", "bash", "zsh", "dash", "ash", "ksh", "fish"}


def _classify_git(tokens):
    if len(tokens) < 2:
        return "read_only"
    sub = tokens[1].lower()
    if sub in _READ_ONLY_GIT_SUBCOMMANDS and sub not in _DESTRUCTIVE_GIT_SUBCOMMANDS:
        return "read_only"
    if sub in _DESTRUCTIVE_GIT_SUBCOMMANDS:
        return "destructive"
    return "workspace_write"


def command_risk_class(command, _depth=0):
    """把 shell 命令粗分成四个类别：read_only / workspace_write / destructive / external_effect。

    只看命令头（第一个词）和几种常见的子命令。真正的沙箱决策要靠 approval 层。
    """
    if command is None:
        return "workspace_write"
    text = str(command).strip()
    if not text:
        return "workspace_write"
    # 深度守卫：递归 shell wrapper 或命令替换过深，直接按最严格类别兜底。
    if _depth >= _MAX_SHELL_WRAPPER_DEPTH:
        return "destructive"
    # 出现命令替换/子 shell（`$(...)`、`` `...` ``、`(...)`），拆出所有内嵌命令
    # 用同一分类器递归，取最严格。
    subshell_verdict = _classify_subshell_content(text, _depth)
    if subshell_verdict is not None:
        return subshell_verdict
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except (TypeError, ValueError):
        tokens = text.split()
    if not tokens:
        return "workspace_write"
    if any(token in _COMPOSITE_OPERATORS for token in tokens):
        return _classify_composite_shell(tokens, _depth)
    head = Path(tokens[0]).name.lower()

    if head in _SHELL_WRAPPERS:
        return _classify_shell_wrapper(tokens, _depth)
    if head == "git":
        return _classify_git(tokens)
    if head in _DESTRUCTIVE_COMMANDS:
        return "destructive"
    if head in _EXTERNAL_EFFECT_COMMANDS:
        return "external_effect"
    if head in _READ_ONLY_COMMANDS:
        return "read_only"
    return "workspace_write"


def _classify_subshell_content(text, depth):
    """把 $(...)、`...`、(...) 里的内容拆出来递归分类。

    只做粗略括号/反引号匹配，够撑住恶意常见形态：
      `curl x | sh`, `$(rm -rf x)`, `echo hi > /etc/hosts`
    """
    verdicts = []
    for match in re.finditer(r"\$\((.*?)\)|`([^`]*)`", text, flags=re.DOTALL):
        payload = match.group(1) if match.group(1) is not None else match.group(2)
        if payload and payload.strip():
            verdicts.append(command_risk_class(payload, _depth=depth + 1))
    if not verdicts:
        return None
    return _worst_risk(verdicts)


def _classify_shell_wrapper(tokens, depth):
    """sh -c "..." / bash -c "..." → 递归分类内部命令。

    包装本身不加分不减分：内部是 read_only 就 read_only，是 destructive
    就 destructive。这样才能挡住 `sh -c 'rm -rf x'` 走 workspace_write。
    也要挡住 bash -lc 'rm -rf x'：任何形如 -*c* 的短 flag 组合都算带 -c。
    """
    inner = _extract_dash_c_payload(tokens)
    if inner is None:
        # 没有 -c 载荷但已经是 wrapper，无法判断实际执行了什么，按最保守 destructive 兜底。
        # 之前默认 workspace_write 太宽松，`bash script.sh` 之类由使用方显式登记。
        return "workspace_write"
    return command_risk_class(inner, _depth=depth + 1)


def _extract_dash_c_payload(tokens):
    """支持 `-c cmd`、`-lc cmd`、`-ec cmd`、`-lic cmd` 等组合短 flag。

    返回紧跟在“带 c 的短 flag”后面第一个非 flag token 作为 payload。
    """
    for index, token in enumerate(tokens):
        if _looks_like_dash_c_flag(token) and index + 1 < len(tokens):
            payload = tokens[index + 1]
            if not payload.startswith("-"):
                return payload
    return None


def _looks_like_dash_c_flag(token):
    if not isinstance(token, str):
        return False
    if not token.startswith("-") or token.startswith("--") or len(token) < 2:
        return False
    return "c" in token[1:]


def _worst_risk(risks):
    order = ("read_only", "workspace_write", "external_effect", "destructive")
    result = "read_only"
    result_index = 0
    for risk in risks:
        index = order.index(risk) if risk in order else 1
        if index > result_index:
            result = risk
            result_index = index
    return result


def _classify_composite_shell(tokens, depth=0):
    """按 | || && ; & 拆段；> >> < << 视作重定向；不认识的段按 workspace_write。"""
    command_risks = []
    current_command = []
    saw_output_redirect = False
    next_is_redirect_target = False
    for token in tokens:
        if token in {">", ">>"}:
            next_is_redirect_target = True
            saw_output_redirect = True
            continue
        if token in {"<", "<<"}:
            next_is_redirect_target = True
            continue
        if token in {"|", "||", "&&", ";", "&"}:
            if current_command:
                command_risks.append(_classify_simple_tokens(current_command, depth))
                current_command = []
            next_is_redirect_target = False
            continue
        if next_is_redirect_target:
            if _redirect_target_is_outside_workspace(token):
                return "destructive"
            next_is_redirect_target = False
            continue
        current_command.append(token)
    if current_command:
        command_risks.append(_classify_simple_tokens(current_command, depth))
    if not command_risks:
        return "workspace_write"
    worst = _worst_risk(command_risks)
    # 输出重定向本身意味着落盘写入，起码是 workspace_write，不允许读命令降级带跑。
    if saw_output_redirect:
        worst = _worst_risk([worst, "workspace_write"])
    return worst


def _classify_simple_tokens(tokens, depth=0):
    if not tokens:
        return "workspace_write"
    head = Path(tokens[0]).name.lower()
    normalized = [head, *tokens[1:]]
    if head in _SHELL_WRAPPERS:
        return _classify_shell_wrapper(normalized, depth)
    if head == "git":
        return _classify_git(normalized)
    if head in _DESTRUCTIVE_COMMANDS:
        return "destructive"
    if head in _EXTERNAL_EFFECT_COMMANDS:
        return "external_effect"
    if head in _READ_ONLY_COMMANDS:
        return "read_only"
    return "workspace_write"


def _redirect_target_is_outside_workspace(token):
    text = str(token)
    return text.startswith("/") or text == ".." or text.startswith("../") or "/../" in text


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
