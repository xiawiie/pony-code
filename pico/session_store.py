"""Session JSON persistence."""

import json
import tempfile
import time
import uuid
from pathlib import Path

from . import file_lock


def _identity(value):
    return value


def _migrate_v1_to_v2(session: dict) -> dict:
    if session.get("schema_version", 1) >= 2:
        return session
    old_history = session.pop("history", [])
    messages = []
    for entry in old_history:
        role = entry.get("role")
        created_at = entry.get("created_at")
        if role == "tool":
            tool_use_id = f"toolu_migrated_{uuid.uuid4().hex[:12]}"
            messages.append({
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": entry.get("name", ""),
                    "input": entry.get("args", {}),
                }],
                "_pico_meta": {"created_at": created_at, "tool_use_id": tool_use_id},
            })
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": entry.get("content", ""),
                }],
                "_pico_meta": {"created_at": created_at, "tool_use_id": tool_use_id},
            })
        elif role in ("user", "assistant"):
            messages.append({
                "role": role,
                "content": entry.get("content", ""),
                "_pico_meta": {"created_at": created_at},
            })
    session["messages"] = messages
    session.setdefault("recently_recalled", [])
    session["schema_version"] = 2
    return session


def _write_backup(session_path, raw_bytes, session_id):
    backup_dir = session_path.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Task A4: nanosecond precision prevents same-second filename collisions.
    ts = time.time_ns()
    (backup_dir / f"{session_id}.v1.{ts}.json").write_bytes(raw_bytes)


class SessionStore:
    def __init__(self, root, redactor=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".session_store.lock"
        self._redactor = redactor or _identity

    def set_redactor(self, redactor):
        self._redactor = redactor or _identity

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def path_for(self, session_id):
        return self.path(session_id)

    def save(self, session):
        path = self.path(session["id"])
        payload = self._redactor(session)
        with file_lock.locked_file(self.lock_path):
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=str(path.parent),
                prefix=path.name + ".",
                suffix=".tmp",
            ) as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                temp_name = handle.name
            Path(temp_name).replace(path)
        return path

    def load(self, session_id):
        p = self.path(session_id)
        raw = p.read_bytes()
        session = json.loads(raw.decode("utf-8"))
        if session.get("schema_version", 1) < 2:
            _write_backup(p, raw, session_id)
            session = _migrate_v1_to_v2(session)
            # 立即写回升级后的格式
            p.write_text(json.dumps(session), encoding="utf-8")
        return session

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None
