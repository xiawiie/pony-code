"""工作区快照工具。

这个模块负责在 agent 按需读文件之前，先给它一份便宜的“仓库第一印象”。
这份快照刻意保持小而稳定：主要包含 Git 事实和少量白名单项目文档。
"""

import hashlib
import json
import os
import stat
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from pony.security import private_files as private_files
from pony.security import paths as security_paths
from pony.security import redaction as redaction
from pony.security import workspace_files as workspace_files
from pony.tools.subprocess import (
    build_trusted_executables,
    discover_lexical_repo_root,
    run_hardened_git,
)

MAX_TOOL_OUTPUT = 4000
MAX_BOOTSTRAP_FILES = 9
MAX_BOOTSTRAP_FILE_BYTES = 64 * 1024
MAX_BOOTSTRAP_TOTAL_BYTES = 256 * 1024
# 这些文件最可能直接影响 agent 的行动方式。
# 我们不会预加载整个仓库，只会先给模型一小份“导航包”。
DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
IGNORED_PATH_NAMES = {
    ".git",
    ".pony",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
}


def _safe_index_path(root, candidate):
    """Return a lexical in-root, non-sensitive path without following it."""
    root = Path(os.path.abspath(os.fspath(root)))
    candidate = Path(candidate)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(os.fspath(candidate)))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    if security_paths.is_sensitive_path(relative.as_posix()):
        return None
    return candidate


def _safe_index_file(root, candidate):
    candidate = _safe_index_path(root, candidate)
    if candidate is None:
        return None
    try:
        safe = security_paths.require_regular_no_symlink(candidate)
        return safe if safe.lstat().st_nlink == 1 else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def _safe_index_directory(root, candidate):
    candidate = _safe_index_path(root, candidate)
    if candidate is None:
        return None
    current = Path(candidate.anchor)
    mode = None
    for part in candidate.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            return None
        if stat.S_ISLNK(mode):
            return None
        if current != candidate and not stat.S_ISDIR(mode):
            return None
    return candidate if mode is not None and stat.S_ISDIR(mode) else None


def _read_bounded_workspace_file(root, path, limit, root_identity):
    relative = Path(path).relative_to(root)
    result = workspace_files.read_regular_bytes_anchored(
        root,
        relative,
        max_bytes=limit,
        expected_root_identity=root_identity,
    )
    if not result["exists"]:
        raise FileNotFoundError(path)
    return result["data"]


def now():
    return datetime.now(timezone.utc).isoformat()


def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]


class WorkspaceContext:
    def __init__(
        self,
        cwd,
        repo_root,
        branch,
        default_branch,
        status,
        recent_commits,
        project_docs,
        trusted_executables=None,
        logical_root=None,
    ):
        self.cwd = cwd
        self.repo_root = repo_root
        self.branch = branch
        self.default_branch = default_branch
        self.status = status
        self.recent_commits = recent_commits
        self.project_docs = project_docs
        self.trusted_executables = dict(trusted_executables or {})
        self.logical_root = str(logical_root or "")

    @classmethod
    def build(
        cls,
        cwd,
        repo_root_override=None,
        executables=None,
        *,
        inspect_git=True,
        logical_root=None,
        branch_override=None,
        default_branch_override=None,
        status_override=None,
    ):
        cwd = Path(cwd).resolve()
        lexical_root = discover_lexical_repo_root(cwd) if inspect_git else cwd
        trusted_executables = (
            build_trusted_executables(lexical_root)
            if executables is None
            else dict(executables)
        )
        git_executable = trusted_executables.get("git") if inspect_git else None

        def git(args, fallback="", *, git_cwd, empty=None):
            if not git_executable:
                return fallback
            try:
                result = run_hardened_git(
                    git_executable,
                    args,
                    cwd=git_cwd,
                    text=True,
                    check=True,
                    timeout=5,
                )
                output = result.stdout.strip()
                if output:
                    return output
                return fallback if empty is None else empty
            except Exception:
                return fallback

        if repo_root_override is not None:
            repo_root = Path(repo_root_override).resolve()
        else:
            reported_root = Path(
                git(
                    ["rev-parse", "--show-toplevel"],
                    str(lexical_root),
                    git_cwd=lexical_root,
                )
            ).resolve()
            repo_root = reported_root if reported_root == lexical_root else lexical_root
        docs = {}
        docs_bytes = 0
        root_identity = private_files.private_directory_identity(repo_root)

        def add_doc(key, data, snippet_limit):
            nonlocal docs_bytes
            if len(docs) >= MAX_BOOTSTRAP_FILES:
                return
            if docs_bytes + len(data) > MAX_BOOTSTRAP_TOTAL_BYTES:
                return
            text = data.decode("utf-8", errors="replace")
            docs[key] = clip(redaction.redact_text(text), snippet_limit)
            docs_bytes += len(data)

        # 同时扫描 repo_root 和 cwd，这样在子目录启动时也能看到本地文档；
        # 但用相对路径做 key，避免同一份文档被重复收集。
        for base in (repo_root, cwd):
            for name in DOC_NAMES:
                path = base / name
                safe_path = _safe_index_file(repo_root, path)
                if safe_path is None:
                    continue
                key = str(safe_path.relative_to(repo_root))
                if key in docs:
                    continue
                try:
                    data = _read_bounded_workspace_file(
                        repo_root,
                        safe_path,
                        MAX_BOOTSTRAP_FILE_BYTES,
                        root_identity,
                    )
                    add_doc(key, data, 1200)
                except (OSError, ValueError):
                    continue

        # v2: 加载 ~/.pony/AGENTS.md 作为全局约定（可选，不存在或不可读时安静跳过）
        # 在函数内 lazy 求值 Path.home()，方便测试用 monkeypatch 隔离本机 home。
        try:
            global_agents_md = Path.home() / ".pony" / "AGENTS.md"
            global_agents_md = security_paths.require_regular_no_symlink(
                global_agents_md
            )
            data = private_files.read_private_bytes(
                global_agents_md,
                max_bytes=MAX_BOOTSTRAP_FILE_BYTES,
                harden=False,
                allow_insecure_mode=True,
            )
            add_doc("<global>/AGENTS.md", data, 1500)
        except (OSError, RuntimeError, ValueError):
            pass

        return cls(
            cwd=str(cwd),
            repo_root=str(repo_root),
            branch=(
                str(branch_override)
                if branch_override is not None
                else git(["branch", "--show-current"], "-", git_cwd=repo_root) or "-"
            ),
            default_branch=(
                str(default_branch_override)
                if default_branch_override is not None
                else (
                    lambda branch: (
                        branch[len("origin/") :]
                        if branch.startswith("origin/")
                        else branch
                    )
                )(
                    git(
                        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                        "origin/main",
                        git_cwd=repo_root,
                    )
                    or "origin/main"
                )
            ),
            status=(
                str(status_override)
                if status_override is not None
                else clip(
                    git(
                        ["status", "--short"],
                        "(unavailable)",
                        git_cwd=repo_root,
                        empty="clean",
                    ),
                    1500,
                )
            ),
            recent_commits=[],
            project_docs=docs,
            trusted_executables=trusted_executables,
            logical_root=logical_root,
        )

    def stable_text(self):
        """Legacy diagnostic view of stable workspace facts and bootstrap docs."""
        docs = (
            "\n".join(
                f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()
            )
            or "- none"
        )
        display_root = self.logical_root or self.repo_root
        display_cwd = display_root
        if self.logical_root:
            try:
                relative = Path(self.cwd).relative_to(Path(self.repo_root))
                if relative.parts:
                    display_cwd = (Path(display_root) / relative).as_posix()
            except ValueError:
                display_cwd = display_root
        return textwrap.dedent(
            f"""\
            Workspace:
            - cwd: {display_cwd}
            - repo_root: {display_root}
            - default_branch: {self.default_branch}
            - project_docs:
            {docs}
            """
        ).strip()

    def instruction_text(self):
        """Render only applicable AGENTS instructions for the pinned prefix.

        README and package metadata are useful context, but they are ordinary
        project sources rather than permanent instructions.  They are allocated
        by the dynamic project-structure source instead.
        """
        instructions = [
            (path, snippet)
            for path, snippet in self.project_docs.items()
            if path == "AGENTS.md"
            or path == "<global>/AGENTS.md"
            or path.replace("\\", "/").endswith("/AGENTS.md")
        ]
        if not instructions:
            return "Project instructions:\n- none"
        docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in instructions)
        return "Project instructions:\n" + docs

    def volatile_text(self):
        """易变部分：branch, status, recent_commits。塞 volatile section。"""
        commits = "\n".join(f"- {line}" for line in self.recent_commits) or "- none"
        return textwrap.dedent(
            f"""\
            <workspace_state>
            - branch: {self.branch}
            - status:
            {self.status}
            - recent_commits:
            {commits}
            </workspace_state>
            """
        ).strip()

    def text(self):
        """Legacy full text (stable + volatile)。为 backward compat 保留。"""
        return self.stable_text() + "\n" + self.volatile_text()

    def fingerprint(self):
        # The fingerprint refreshes both pinned instructions and dynamic sources.
        payload = {
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "status": self.status,
            "recent_commits": list(self.recent_commits),
            "project_docs": dict(self.project_docs),
            "logical_root": self.logical_root,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
