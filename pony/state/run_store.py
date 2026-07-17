"""运行工件落盘。

Session JSONL Tree 负责保存“可恢复的会话状态”；RunStore 负责保存“单次运行的审计工件”，
例如 task_state、trace 和 report。两者分开后，恢复现场和复盘证据不会混在一起。
"""

from copy import deepcopy
import json
import re

from pony.state import file_lock
from pony.agent.observability import (
    MAX_RUN_ARTIFACT_BYTES,
    _decode_json,
    validate_report,
)
from pony.security.private_files import (
    append_private_bytes,
    ensure_private_dir,
    harden_private_tree,
    private_directory_identity,
    read_private_text,
    write_private_bytes_atomic,
)


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _run_id(value):
    if hasattr(value, "run_id"):
        value = value.run_id
    run_id = str(value or "")
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("invalid run id")
    return run_id


def _identity(value):
    return value


class RunStore:
    def __init__(self, root, redactor=None):
        self.root = harden_private_tree(root)
        self._root_identity = private_directory_identity(self.root)
        self._redactor = redactor or _identity
        self._redactor_configured = redactor is not None

    def set_redactor(self, redactor):
        self._redactor = redactor or _identity
        self._redactor_configured = redactor is not None

    def run_dir(self, run_id):
        return self.root / _run_id(run_id)

    def task_state_path(self, run_id):
        return self.run_dir(run_id) / "task_state.json"

    def trace_path(self, run_id):
        return self.run_dir(run_id) / "trace.jsonl"

    def report_path(self, run_id):
        return self.run_dir(run_id) / "report.json"

    def trace_lock_path(self, run_id):
        return self.run_dir(run_id) / ".trace.lock"

    def start_run(self, task_state):
        # 每次 ask() 都会生成一个 run 目录。
        # 这样一次用户请求对应一组独立工件，后续排查更容易。
        run_dir = self.run_dir(task_state)
        self.write_task_state(task_state)
        return run_dir

    def write_task_state(self, task_state):
        path = self.task_state_path(task_state)
        payload = self._redactor(deepcopy(task_state.to_dict()))
        self._write_json_atomic(path, payload)
        return path

    def append_trace(self, task_state, event):
        path = self.trace_path(task_state)
        serialized = (
            json.dumps(
            self._redactor(deepcopy(event)),
            sort_keys=True,
            ensure_ascii=True,
            )
            + "\n"
        )
        ensure_private_dir(path.parent)
        # trace 采用 jsonl 追加写入，原因是 agent 运行过程是流式事件序列，
        # 逐条落盘比“最后一次性写整份 trace”更稳，也更适合调试。
        with file_lock.locked_file(
            self.trace_lock_path(task_state),
            require_lock=True,
        ):
            return append_private_bytes(
                path,
                serialized.encode("utf-8"),
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_total_bytes=MAX_RUN_ARTIFACT_BYTES,
            )

    def write_report(self, task_state, report):
        path = self.report_path(task_state)
        payload = self._redactor(deepcopy(report))
        self._write_json_atomic(path, payload)
        return path

    def load_task_state(self, task_id):
        return _decode_json(
            read_private_text(
                self.task_state_path(task_id),
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_bytes=MAX_RUN_ARTIFACT_BYTES,
            )
        )

    def load_report(self, task_id):
        run_id = _run_id(task_id)
        report = _decode_json(
            read_private_text(
                self.report_path(run_id),
                trusted_root=self.root,
                trusted_root_identity=self._root_identity,
                max_bytes=MAX_RUN_ARTIFACT_BYTES,
            )
        )
        return validate_report(report, run_id=run_id)

    def _write_json_atomic(self, path, payload):
        # 原子写：先写临时文件，再 replace。
        # 这样即使中途异常，也不容易留下半截 JSON。
        rendered = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        if len(rendered) > MAX_RUN_ARTIFACT_BYTES:
            raise ValueError("private file too large")
        ensure_private_dir(path.parent)
        write_private_bytes_atomic(
            path,
            rendered,
            trusted_root=self.root,
            trusted_root_identity=self._root_identity,
            error="run temp changed",
            max_existing_bytes=MAX_RUN_ARTIFACT_BYTES,
        )
