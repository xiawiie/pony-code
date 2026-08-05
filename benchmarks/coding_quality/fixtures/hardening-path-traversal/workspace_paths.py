from pathlib import Path


def resolve_workspace_path(root, raw_path):
    return (Path(root) / raw_path).resolve()
