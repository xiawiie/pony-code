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

from pico import security as securitylib
from pico.safe_subprocess import (
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
IGNORED_PATH_NAMES = {".git", ".pico", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}


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
    if securitylib.is_sensitive_path(relative.as_posix()):
        return None
    return candidate


def _safe_index_file(root, candidate):
    candidate = _safe_index_path(root, candidate)
    if candidate is None:
        return None
    try:
        return securitylib.require_regular_no_symlink(candidate)
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


def _read_bounded_regular(path, limit):
    path = Path(os.path.abspath(os.fspath(path)))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    parent_descriptor = securitylib._open_private_directory(path.parent)
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        current = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    finally:
        os.close(parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        path_current = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or (opened.st_dev, opened.st_ino)
            != (path_current.st_dev, path_current.st_ino)
            or opened.st_size > limit
        ):
            raise ValueError("unsafe automatic workspace file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError("automatic workspace file is too large")
        return data
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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
            build_trusted_executables(lexical_root) if executables is None else dict(executables)
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

        def add_doc(key, path, snippet_limit):
            nonlocal docs_bytes
            if len(docs) >= MAX_BOOTSTRAP_FILES:
                return
            data = _read_bounded_regular(path, MAX_BOOTSTRAP_FILE_BYTES)
            if docs_bytes + len(data) > MAX_BOOTSTRAP_TOTAL_BYTES:
                return
            text = data.decode("utf-8", errors="replace")
            docs[key] = clip(securitylib.redact_text(text), snippet_limit)
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
                    add_doc(key, safe_path, 1200)
                except (OSError, ValueError):
                    continue

        # v2: 加载 ~/.pico/AGENTS.md 作为全局约定（可选，不存在或不可读时安静跳过）
        # 在函数内 lazy 求值 Path.home()，方便测试用 monkeypatch 隔离本机 home。
        try:
            global_agents_md = Path.home() / ".pico" / "AGENTS.md"
            global_agents_md = securitylib.require_regular_no_symlink(global_agents_md)
            add_doc("<global>/AGENTS.md", global_agents_md, 1500)
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
                    lambda branch: branch[len("origin/") :]
                    if branch.startswith("origin/")
                    else branch
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
        """稳定部分：cwd, repo_root, default_branch, project_docs。塞 stable prefix。"""
        docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()) or "- none"
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
        # 这个指纹用来判断仓库状态是否发生了足够大的变化，
        # 从而决定是否需要重建缓存中的 prompt prefix。
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
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
