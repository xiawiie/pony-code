"""Small runtime working memory model."""

from .features.memory import canonicalize_path

TASK_SUMMARY_LIMIT = 300
RECENT_FILES_LIMIT = 8


def _truncate(text, limit):
    text = str(text)
    return text[:limit]


def _normalize_task_summary(summary, limit):
    if summary is None:
        return ""
    return _truncate(str(summary).strip(), limit)


def _ensure_file_list(value):
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dedupe_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


class WorkingMemory:
    TASK_SUMMARY_LIMIT = TASK_SUMMARY_LIMIT
    RECENT_FILES_LIMIT = RECENT_FILES_LIMIT

    def __init__(self, task_summary="", recent_files=None, workspace_root=None):
        self.workspace_root = workspace_root
        self.task_summary = _normalize_task_summary(task_summary, self.TASK_SUMMARY_LIMIT)
        self.recent_files = _dedupe_preserve_order(
            [self.canonical_path(path).strip() for path in _ensure_file_list(recent_files or [])]
        )[: self.RECENT_FILES_LIMIT]

    def to_dict(self):
        return {
            "task_summary": self.task_summary,
            "recent_files": list(self.recent_files),
        }

    @classmethod
    def from_dict(cls, data, workspace_root=None):
        if not isinstance(data, dict):
            return cls(workspace_root=workspace_root)

        source = data
        if isinstance(data.get("working"), dict):
            source = data["working"]

        task_summary = source.get("task_summary", source.get("task", ""))
        if not isinstance(task_summary, str):
            task_summary = ""
        recent_files = source.get("recent_files", source.get("files", []))
        return cls(task_summary=task_summary, recent_files=recent_files, workspace_root=workspace_root)

    def canonical_path(self, path):
        return canonicalize_path(path, self.workspace_root)

    def set_task_summary(self, summary):
        self.task_summary = _normalize_task_summary(summary, self.TASK_SUMMARY_LIMIT)
        return self

    def remember_file(self, path):
        path = self.canonical_path(path).strip()
        if not path:
            return self
        self.recent_files = [item for item in self.recent_files if item != path]
        self.recent_files.insert(0, path)
        self.recent_files = self.recent_files[: self.RECENT_FILES_LIMIT]
        return self
