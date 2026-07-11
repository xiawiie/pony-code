"""Production helpers for Pico's compact file-summary memory."""

import hashlib as _hashlib
from pathlib import Path as _Path

from ..workspace import clip as _clip, now as _now

__all__ = [
    "canonicalize_path",
    "file_freshness",
    "normalize_file_summaries_dict",
    "set_file_summary_dict",
    "invalidate_file_summary_dict",
    "invalidate_stale_file_summaries_dict",
    "summarize_read_result",
]


def _resolve_workspace_path(raw_path, workspace_root=None):
    path = _Path(str(raw_path))
    if workspace_root is None:
        return path

    root = _Path(workspace_root).resolve()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def canonicalize_path(raw_path, workspace_root=None):
    resolved = _resolve_workspace_path(raw_path, workspace_root)
    if resolved is None or workspace_root is None:
        return _Path(str(raw_path)).as_posix()
    return resolved.relative_to(_Path(workspace_root).resolve()).as_posix()


def file_freshness(raw_path, workspace_root=None):
    resolved = _resolve_workspace_path(raw_path, workspace_root)
    if resolved is None or not resolved.exists() or not resolved.is_file():
        return None
    return _hashlib.sha256(resolved.read_bytes()).hexdigest()


def _canonical_file_summary_path(path, workspace_root=None):
    if not str(path).strip():
        return ""
    return canonicalize_path(path, workspace_root).strip()


def normalize_file_summaries_dict(summaries, workspace_root=None):
    if not isinstance(summaries, dict):
        return {}

    normalized = {}
    for path, summary in summaries.items():
        path = _canonical_file_summary_path(path, workspace_root)
        if isinstance(summary, dict):
            text = _clip(str(summary.get("summary", "")).strip(), 500)
            created_at = str(summary.get("created_at", "")).strip() or _now()
            freshness = summary.get("freshness")
        else:
            text = _clip(str(summary).strip(), 500)
            created_at = _now()
            freshness = file_freshness(path, workspace_root)
        if not path or not text:
            continue
        normalized[path] = {
            "summary": text,
            "created_at": created_at,
            "freshness": freshness,
        }
    return normalized


def set_file_summary_dict(summaries, path, summary, workspace_root=None):
    if not isinstance(summaries, dict):
        return {}
    path = _canonical_file_summary_path(path, workspace_root)
    summary = _clip(str(summary).strip(), 500)
    if not path or not summary:
        return summaries
    summaries[path] = {
        "summary": summary,
        "created_at": _now(),
        "freshness": file_freshness(path, workspace_root),
    }
    return summaries


def invalidate_file_summary_dict(summaries, path, workspace_root=None):
    if not isinstance(summaries, dict):
        return {}
    path = _canonical_file_summary_path(path, workspace_root)
    if path:
        summaries.pop(path, None)
    return summaries


def invalidate_stale_file_summaries_dict(summaries, workspace_root=None):
    if not isinstance(summaries, dict):
        return []
    invalidated = []
    for path, summary in list(summaries.items()):
        current_freshness = file_freshness(path, workspace_root)
        if isinstance(summary, dict) and summary.get("freshness") == current_freshness:
            continue
        invalidated.append(path)
        summaries.pop(path, None)
    return invalidated


def summarize_read_result(result, limit=180):
    lines = [line.strip() for line in str(result).splitlines() if line.strip()]
    if lines[:1] and lines[0].startswith("# "):
        lines = lines[1:]
    if not lines:
        return "(empty)"
    return _clip(" | ".join(lines[:3]), limit)
