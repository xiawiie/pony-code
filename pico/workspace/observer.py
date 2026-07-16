"""在 run_shell 前后各拍一张 workspace 快照，用于算出这次命令改了哪些文件。

Git 仓库里用 `git status --porcelain=v1 -z -uall` 的输出打底，非 Git 目录用普通
mtime+size+存在性扫描兜底。两种模式返回同一个结构，`diff()` 只把“真的变了”的
路径拿出来。
"""

import os
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from pico.tools.subprocess import run_hardened_git
from pico.security.paths import require_regular_no_symlink
from pico.workspace.context import (
    _safe_index_directory,
    _safe_index_file,
    _safe_index_path,
)


_MAX_FILE_SIZE_FOR_MTIME = 8 * 1024 * 1024


def _validated_legacy_git(root, value):
    try:
        candidate = Path(os.fspath(value))
    except TypeError:
        return ""
    if not candidate.is_absolute():
        return ""
    try:
        executable = candidate.resolve(strict=True)
        require_regular_no_symlink(executable)
        mode = executable.stat().st_mode
        workspace = Path(root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return ""
    if executable == workspace or workspace in executable.parents:
        return ""
    if mode & (stat.S_IWGRP | stat.S_IWOTH) or not os.access(executable, os.X_OK):
        return ""
    return str(executable)


class WorkspaceObserver:
    def __init__(self, root, git_binary=None, *, executables=None):
        self.root = Path(os.path.abspath(os.fspath(root)))
        if executables is None and isinstance(git_binary, Mapping):
            trusted = dict(git_binary)
            git_binary = None
        else:
            trusted = dict(executables or {})
        if "git" not in trusted:
            legacy_git = _validated_legacy_git(self.root, git_binary)
            if legacy_git:
                trusted["git"] = legacy_git
        self.trusted_executables = MappingProxyType(trusted)

    def _is_git_repo(self):
        git_executable = self.trusted_executables.get("git")
        if not git_executable:
            return False
        try:
            result = run_hardened_git(
                git_executable,
                ["rev-parse", "--is-inside-work-tree"],
                cwd=self.root,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _capture_git(self):
        # git status 给出每一个非干净路径的状态。Phase 1 里我们把状态 token 也
        # 当作哈希前缀存起来，用来在 diff 时区分 clean 和 dirty。
        git_executable = self.trusted_executables.get("git")
        if not git_executable:
            return self._capture_filesystem()
        try:
            proc = run_hardened_git(
                git_executable,
                ["status", "--porcelain=v1", "-z", "-uall"],
                cwd=self.root,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return self._capture_filesystem()
        if proc.returncode != 0:
            return self._capture_filesystem()
        raw = proc.stdout or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", errors="replace")
        paths = {}
        entries = raw.split(b"\x00")
        i = 0
        while i < len(entries):
            entry = entries[i]
            if not entry:
                i += 1
                continue
            status = entry[:2].decode("ascii", errors="replace")
            path = entry[3:].decode("utf-8", errors="replace")
            if status.startswith("R"):
                # renames give two entries separated by NUL
                original = (
                    entries[i + 1].decode("utf-8", errors="replace")
                    if i + 1 < len(entries)
                    else ""
                )
                original_path = _safe_index_path(self.root, self.root / original)
                renamed_path = _safe_index_path(self.root, self.root / path)
                if original_path is not None:
                    paths[original_path.relative_to(self.root).as_posix()] = "R:removed"
                if renamed_path is not None:
                    paths[renamed_path.relative_to(self.root).as_posix()] = "R:added"
                i += 2
                continue
            candidate = _safe_index_path(self.root, self.root / path)
            if candidate is not None:
                paths[candidate.relative_to(self.root).as_posix()] = (
                    status.strip() or "?"
                )
            i += 1
        # 也顺带记录每个 dirty 文件的 size+mtime，用来精确 diff
        detail = {}
        for path in list(paths.keys()):
            abs_path = _safe_index_file(self.root, self.root / path)
            if abs_path is not None:
                try:
                    stat = abs_path.stat()
                except OSError:
                    continue
                detail[path] = f"{stat.st_size}:{int(stat.st_mtime * 1_000_000)}"
        return {"mode": "git", "paths": paths, "detail": detail, "summaries": []}

    def _capture_filesystem(self):
        paths = {}
        root = _safe_index_directory(self.root, self.root)
        if root is None:
            return {
                "mode": "filesystem",
                "paths": paths,
                "detail": paths,
                "summaries": [],
            }
        for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
            # 跳过常见的构建产物和虚拟环境目录，避免把无关文件也算成 delta
            dirnames[:] = [
                name
                for name in dirnames
                if name not in {".git", ".pico", "__pycache__", ".venv", "node_modules"}
                and _safe_index_directory(root, Path(dirpath) / name) is not None
            ]
            for filename in filenames:
                abs_path = _safe_index_file(root, Path(dirpath) / filename)
                if abs_path is None:
                    continue
                try:
                    rel = abs_path.relative_to(root).as_posix()
                except ValueError:
                    continue
                try:
                    stat = abs_path.stat()
                except OSError:
                    continue
                if stat.st_size > _MAX_FILE_SIZE_FOR_MTIME:
                    marker = f"large:{stat.st_size}"
                else:
                    marker = f"{stat.st_size}:{int(stat.st_mtime * 1_000_000)}"
                paths[rel] = marker
        return {"mode": "filesystem", "paths": paths, "detail": paths, "summaries": []}

    def capture(self):
        if _safe_index_directory(self.root, self.root) is None:
            return self._capture_filesystem()
        if self._is_git_repo():
            return self._capture_git()
        return self._capture_filesystem()

    def capture_call_start(self):
        return self.capture()

    def capture_call_end(self):
        return self.capture()

    def invalidate_call_cache(self):
        return None

    def diff(self, before, after):
        """比较两次 capture，只报告真的发生变化的路径。"""
        before_paths = dict(before.get("paths", {}))
        after_paths = dict(after.get("paths", {}))
        before_detail = dict(before.get("detail", {}))
        after_detail = dict(after.get("detail", {}))

        mode = after.get("mode") or before.get("mode") or "filesystem"

        # 用两侧 keys ∪，得到所有候选路径
        candidates = (
            set(before_paths.keys())
            | set(after_paths.keys())
            | set(before_detail.keys())
            | set(after_detail.keys())
        )
        changed = []
        summaries = []

        for path in sorted(candidates):
            before_marker = before_detail.get(path) or before_paths.get(path)
            after_marker = after_detail.get(path) or after_paths.get(path)
            if before_marker == after_marker:
                continue
            # 判断存在性变化
            after_exists = _safe_index_file(self.root, self.root / path) is not None
            before_had_marker = before_marker is not None
            after_had_marker = after_marker is not None
            if not before_had_marker and after_had_marker and after_exists:
                summaries.append(f"created:{path}")
            elif before_had_marker and not after_exists:
                summaries.append(f"deleted:{path}")
            elif before_had_marker and after_had_marker:
                summaries.append(f"modified:{path}")
            else:
                # git 报了状态但文件不在（删除后被 git 追踪）→ 视作删除
                summaries.append(f"deleted:{path}")
            changed.append(path)

        return {
            "mode": mode,
            "changed_paths": changed,
            "summaries": summaries,
        }
