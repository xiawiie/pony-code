from pathlib import Path


def resolve_workspace_path(root, raw_path):
    root = Path(root).resolve()
    candidate = Path(raw_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("path escapes workspace")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes workspace") from exc
    return resolved
