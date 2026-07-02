"""可恢复编辑的落盘层。

目录约定（相对 workspace_root）：
    .pico/checkpoints/records/          # Checkpoint Record，一份 turn/restore/manual 的元数据
    .pico/checkpoints/tool_changes/     # Tool Change Record，逐次工具执行的元数据
    .pico/checkpoints/blobs/            # 原始字节内容，按 sha256 前两位分桶

所有写入都走原子 replace，防止在崩溃时留下半截 JSON。
"""

from datetime import datetime, timedelta, timezone
import json
import re
import tempfile
from pathlib import Path

from pico.recovery_paths import hash_bytes


class CheckpointStore:
    def __init__(self, workspace_root):
        # workspace_root 通常就是 Pico 的 repo 根。真实存储放在 .pico/checkpoints 下。
        # 如果传入路径已经是 .pico/checkpoints，直接用；否则加子目录。
        workspace_root = Path(workspace_root)
        if workspace_root.name == "checkpoints" and workspace_root.parent.name == ".pico":
            self.root = workspace_root
        else:
            self.root = workspace_root / ".pico" / "checkpoints"
        self.records_dir = self.root / "records"
        self.tool_changes_dir = self.root / "tool_changes"
        self.blobs_dir = self.root / "blobs"
        for directory in (self.records_dir, self.tool_changes_dir, self.blobs_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # -- blob 存取 --------------------------------------------------------
    def _blob_path(self, content_hash):
        return self.blobs_dir / content_hash[:2] / content_hash

    def write_blob(self, data, content_kind="text"):
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("write_blob requires bytes-like data")
        info = hash_bytes(bytes(data))
        blob_ref = info["content_hash"]
        blob_path = self._blob_path(blob_ref)
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        if not blob_path.exists():
            with tempfile.NamedTemporaryFile(delete=False, dir=str(blob_path.parent), prefix=blob_ref + ".", suffix=".tmp") as handle:
                handle.write(data)
                temp_name = handle.name
            Path(temp_name).replace(blob_path)
        return {
            "blob_ref": blob_ref,
            "content_hash": blob_ref,
            "hash_algorithm": info["hash_algorithm"],
            "size_bytes": info["size_bytes"],
            "content_kind": content_kind,
        }

    def read_blob(self, blob_ref):
        return self._blob_path(str(blob_ref)).read_bytes()

    def has_blob(self, blob_ref):
        return self._blob_path(str(blob_ref)).exists()

    # -- checkpoint record ------------------------------------------------
    def _record_path(self, checkpoint_id):
        return self.records_dir / (str(checkpoint_id) + ".json")

    def write_checkpoint_record(self, record):
        checkpoint_id = record["checkpoint_id"]
        path = self._record_path(checkpoint_id)
        self._write_json_atomic(path, record)
        return path

    def load_checkpoint_record(self, checkpoint_id):
        return json.loads(self._record_path(checkpoint_id).read_text(encoding="utf-8"))

    def list_checkpoint_records(self):
        records = []
        for path in sorted(self.records_dir.glob("*.json")):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        records.sort(key=lambda item: item.get("created_at", ""))
        return records

    # -- tool change record ----------------------------------------------
    def _tool_change_path(self, tool_change_id):
        return self.tool_changes_dir / (str(tool_change_id) + ".json")

    def write_tool_change_record(self, record):
        tool_change_id = record["tool_change_id"]
        path = self._tool_change_path(tool_change_id)
        self._write_json_atomic(path, record)
        return path

    def load_tool_change_record(self, tool_change_id):
        return json.loads(self._tool_change_path(tool_change_id).read_text(encoding="utf-8"))

    def list_tool_change_records(self):
        records = []
        for path in sorted(self.tool_changes_dir.glob("*.json")):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        records.sort(key=lambda item: item.get("started_at", ""))
        return records

    # -- pruning ----------------------------------------------------------
    def prune(self, dry_run=True, older_than=None, now=None):
        """扫描所有 blob 引用，返回未被引用的 blob。dry_run=False 时才真的删除。

        引用来源必须囊括：
          - checkpoint record 的 file_entries
          - checkpoint record 的 restore_provenance.pre_restore_file_states 与 post_...
          - tool change record 的 file_entries
        任何一处漏扫，都会误删仍被引用的 blob。
        """
        checkpoint_records = self.list_checkpoint_records()
        tool_change_records = self.list_tool_change_records()
        cutoff = _cutoff_datetime(older_than, now=now)
        prunable_checkpoint_ids = _prunable_checkpoint_ids(checkpoint_records, cutoff)
        prunable_checkpoint_id_set = set(prunable_checkpoint_ids)
        retained_checkpoint_records = [
            record
            for record in checkpoint_records
            if record.get("checkpoint_id") not in prunable_checkpoint_id_set
        ]
        retained_tool_change_ids = {
            tool_change_id
            for record in retained_checkpoint_records
            for tool_change_id in (record.get("tool_change_ids", []) or [])
            if tool_change_id
        }
        candidate_tool_change_ids = {
            tool_change_id
            for record in checkpoint_records
            if record.get("checkpoint_id") in prunable_checkpoint_id_set
            for tool_change_id in (record.get("tool_change_ids", []) or [])
            if tool_change_id
        }
        prunable_tool_change_ids = sorted(candidate_tool_change_ids - retained_tool_change_ids)
        prunable_tool_change_id_set = set(prunable_tool_change_ids)
        retained_tool_change_records = [
            record
            for record in tool_change_records
            if record.get("tool_change_id") not in prunable_tool_change_id_set
        ]

        referenced = _referenced_blob_refs(retained_checkpoint_records, retained_tool_change_records)

        unreferenced = []
        for blob_path in self.blobs_dir.rglob("*"):
            if not blob_path.is_file():
                continue
            blob_ref = blob_path.name
            if not _looks_like_blob_ref(blob_ref):
                continue
            if blob_ref in referenced:
                continue
            unreferenced.append(blob_ref)

        removed_checkpoint_ids = []
        removed_tool_change_ids = []
        removed = []
        if not dry_run:
            for checkpoint_id in prunable_checkpoint_ids:
                try:
                    self._record_path(checkpoint_id).unlink()
                    removed_checkpoint_ids.append(checkpoint_id)
                except OSError:
                    continue
            for tool_change_id in prunable_tool_change_ids:
                try:
                    self._tool_change_path(tool_change_id).unlink()
                    removed_tool_change_ids.append(tool_change_id)
                except OSError:
                    continue
            for blob_ref in unreferenced:
                target = self._blob_path(blob_ref)
                try:
                    target.unlink()
                    removed.append(blob_ref)
                except OSError:
                    continue

        return {
            "dry_run": bool(dry_run),
            "older_than": str(older_than or ""),
            "cutoff_created_before": cutoff.isoformat() if cutoff is not None else "",
            "prunable_checkpoint_ids": prunable_checkpoint_ids,
            "prunable_tool_change_ids": prunable_tool_change_ids,
            "removed_checkpoint_ids": removed_checkpoint_ids,
            "removed_tool_change_ids": removed_tool_change_ids,
            "referenced_count": len(referenced),
            "unreferenced_blob_refs": unreferenced,
            "removed_blob_refs": removed,
        }

    # -- helpers ----------------------------------------------------------
    def _write_json_atomic(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(path)


def _collect_blob_refs(entry, sink):
    if not isinstance(entry, dict):
        return
    for key in ("before_blob_ref", "after_blob_ref", "blob_ref"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            sink.add(value)


def _referenced_blob_refs(checkpoint_records, tool_change_records):
    referenced = set()
    for record in checkpoint_records:
        for entry in record.get("file_entries", []) or []:
            _collect_blob_refs(entry, referenced)
        provenance = record.get("restore_provenance") or {}
        for entry in provenance.get("pre_restore_file_states", []) or []:
            _collect_blob_refs(entry, referenced)
        for entry in provenance.get("post_restore_file_states", []) or []:
            _collect_blob_refs(entry, referenced)
    for record in tool_change_records:
        for entry in record.get("file_entries", []) or []:
            _collect_blob_refs(entry, referenced)
    return referenced


def _cutoff_datetime(older_than, now=None):
    if not older_than:
        return None
    duration = _parse_duration(older_than)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - duration


def _parse_duration(value):
    match = re.fullmatch(r"([1-9][0-9]*)([smhdw])", str(value or "").strip())
    if not match:
        raise ValueError("older_than must use a duration like 7d, 12h, or 30m")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    return timedelta(weeks=amount)


def _prunable_checkpoint_ids(checkpoint_records, cutoff):
    if cutoff is None:
        return []
    parent_refs = {
        str(record.get("parent_checkpoint_id") or "")
        for record in checkpoint_records
        if record.get("parent_checkpoint_id")
    }
    prunable = []
    for record in checkpoint_records:
        checkpoint_id = str(record.get("checkpoint_id") or "")
        if not checkpoint_id or checkpoint_id in parent_refs:
            continue
        created_at = _parse_created_at(record.get("created_at", ""))
        if created_at is not None and created_at < cutoff:
            prunable.append(checkpoint_id)
    return prunable


def _parse_created_at(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _looks_like_blob_ref(value):
    text = str(value)
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)
