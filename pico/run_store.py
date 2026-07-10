"""运行工件落盘。

session.json 负责保存“可恢复的会话状态”；RunStore 负责保存“单次运行的审计工件”，
例如 task_state、trace 和 report。两者分开后，恢复现场和复盘证据不会混在一起。
"""

import json
import os
import stat
import tempfile
from pathlib import Path

from .security import (
    ensure_private_dir,
    ensure_private_file,
    require_regular_no_symlink,
)


def _run_id(value):
    if hasattr(value, "run_id"):
        return value.run_id
    return str(value)


def _identity(value):
    return value


class RunStore:
    def __init__(self, root, redactor=None):
        self.root = ensure_private_dir(root)
        self._redactor = redactor or _identity

    def set_redactor(self, redactor):
        self._redactor = redactor or _identity

    def run_dir(self, run_id):
        return self.root / _run_id(run_id)

    def task_state_path(self, run_id):
        return self.run_dir(run_id) / "task_state.json"

    def trace_path(self, run_id):
        return self.run_dir(run_id) / "trace.jsonl"

    def report_path(self, run_id):
        return self.run_dir(run_id) / "report.json"

    def start_run(self, task_state):
        # 每次 ask() 都会生成一个 run 目录。
        # 这样一次用户请求对应一组独立工件，后续排查更容易。
        run_dir = self.run_dir(task_state)
        ensure_private_dir(run_dir)
        self.write_task_state(task_state)
        return run_dir

    def write_task_state(self, task_state):
        path = self.task_state_path(task_state)
        ensure_private_dir(path.parent)
        self._write_json_atomic(path, self._redactor(task_state.to_dict()))
        return path

    def append_trace(self, task_state, event):
        path = self.trace_path(task_state)
        ensure_private_dir(path.parent)
        checked_path = require_regular_no_symlink(path, allow_missing=True)
        try:
            before = os.stat(checked_path, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        # trace 采用 jsonl 追加写入，原因是 agent 运行过程是流式事件序列，
        # 逐条落盘比“最后一次性写整份 trace”更稳，也更适合调试。
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(checked_path, flags, 0o600)
        try:
            opened = os.fstat(descriptor)
            current = os.stat(checked_path, follow_symlinks=False)
            identity = (opened.st_dev, opened.st_ino)
            if not stat.S_ISREG(opened.st_mode) or (
                current.st_dev,
                current.st_ino,
            ) != identity:
                raise ValueError("trace file changed")
            if before is not None and (before.st_dev, before.st_ino) != identity:
                raise ValueError("trace file changed")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(json.dumps(self._redactor(event), sort_keys=True, ensure_ascii=True))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return ensure_private_file(checked_path)

    def write_report(self, task_state, report):
        path = self.report_path(task_state)
        ensure_private_dir(path.parent)
        self._write_json_atomic(path, self._redactor(report))
        return path

    def load_task_state(self, task_id):
        path = ensure_private_file(
            require_regular_no_symlink(self.task_state_path(task_id))
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def load_report(self, task_id):
        path = ensure_private_file(
            require_regular_no_symlink(self.report_path(task_id))
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json_atomic(self, path, payload):
        # 原子写：先写临时文件，再 replace。
        # 这样即使中途异常，也不容易留下半截 JSON。
        path = require_regular_no_symlink(path, allow_missing=True)
        temp_path = None
        temp_identity = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=str(path.parent),
                prefix=path.name + ".",
                suffix=".tmp",
            ) as handle:
                temp_path = Path(handle.name)
                opened = os.fstat(handle.fileno())
                temp_identity = (opened.st_dev, opened.st_ino)
                os.fchmod(handle.fileno(), 0o600)
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            current = temp_path.lstat()
            if (
                not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != temp_identity
            ):
                raise ValueError("run temp changed")
            temp_path.replace(path)
            ensure_private_file(path)
        finally:
            if temp_path is not None and temp_identity is not None:
                try:
                    current = temp_path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (current.st_dev, current.st_ino) == temp_identity:
                        temp_path.unlink()
