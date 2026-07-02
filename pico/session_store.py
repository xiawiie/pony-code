"""Session JSON persistence."""

import json
import tempfile
from pathlib import Path

from . import file_lock


def _identity(value):
    return value


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
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None
