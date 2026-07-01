"""在 run_shell 前后各拍一张 workspace 快照，用于算出这次命令改了哪些文件。

Git 仓库里用 `git status --porcelain=v1 -z -uall` 的输出打底，非 Git 目录用普通
mtime+size+存在性扫描兜底。两种模式返回同一个结构，`diff()` 只把“真的变了”的
路径拿出来。
"""

import os
import subprocess
from pathlib import Path


_MAX_FILE_SIZE_FOR_MTIME = 8 * 1024 * 1024


class WorkspaceObserver:
    def __init__(self, root, git_binary="git"):
        self.root = Path(root).resolve()
        self.git_binary = git_binary

    def _is_git_repo(self):
        try:
            result = subprocess.run(
                [self.git_binary, "rev-parse", "--is-inside-work-tree"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                check=False,
            )
        except (FileNotFoundError, PermissionError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _capture_git(self):
        # git status 给出每一个非干净路径的状态。Phase 1 里我们把状态 token 也
        # 当作哈希前缀存起来，用来在 diff 时区分 clean 和 dirty。
        proc = subprocess.run(
            [self.git_binary, "status", "--porcelain=v1", "-z", "-uall"],
            cwd=str(self.root),
            capture_output=True,
            check=False,
        )
        raw = proc.stdout or b""
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
                original = entries[i + 1].decode("utf-8", errors="replace") if i + 1 < len(entries) else ""
                paths[original] = "R:removed"
                paths[path] = "R:added"
                i += 2
                continue
            paths[path] = status.strip() or "?"
            i += 1
        # 也顺带记录每个 dirty 文件的 size+mtime，用来精确 diff
        detail = {}
        for path in list(paths.keys()):
            abs_path = self.root / path
            if abs_path.is_file():
                stat = abs_path.stat()
                detail[path] = f"{stat.st_size}:{int(stat.st_mtime * 1_000_000)}"
        return {"mode": "git", "paths": paths, "detail": detail, "summaries": []}

    def _capture_filesystem(self):
        paths = {}
        for dirpath, dirnames, filenames in os.walk(str(self.root)):
            # 跳过常见的构建产物和虚拟环境目录，避免把无关文件也算成 delta
            dirnames[:] = [d for d in dirnames if d not in {".git", ".pico", "__pycache__", ".venv", "node_modules"}]
            for filename in filenames:
                abs_path = Path(dirpath) / filename
                try:
                    rel = abs_path.relative_to(self.root).as_posix()
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
        if self._is_git_repo():
            return self._capture_git()
        return self._capture_filesystem()

    def diff(self, before, after):
        """比较两次 capture，只报告真的发生变化的路径。"""
        before_paths = dict(before.get("paths", {}))
        after_paths = dict(after.get("paths", {}))
        before_detail = dict(before.get("detail", {}))
        after_detail = dict(after.get("detail", {}))

        mode = after.get("mode") or before.get("mode") or "filesystem"

        # 用两侧 keys ∪，得到所有候选路径
        candidates = set(before_paths.keys()) | set(after_paths.keys()) | set(before_detail.keys()) | set(after_detail.keys())
        changed = []
        summaries = []

        for path in sorted(candidates):
            before_marker = before_detail.get(path) or before_paths.get(path)
            after_marker = after_detail.get(path) or after_paths.get(path)
            if before_marker == after_marker:
                continue
            # 判断存在性变化
            after_exists = (self.root / path).exists()
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
