"""路径安全与哈希：所有恢复相关的路径必须通过这里做归一化。

Phase 1 只关心 workspace 内的相对路径。写盘、读盘、比对哈希的入口都走这里，
避免出现绝对路径穿越、symlink 逃逸、Windows/Unix 分隔符不一致的问题。
"""

import hashlib
import os
from pathlib import Path


_TRAVERSAL_TOKENS = {"..", "..\\", "../"}


def normalize_workspace_relative_path(raw_path):
    """把工具接收到的路径归一化成 workspace 相对 POSIX 形式。

    不接受绝对路径、盘符路径、包含 `..` 的向上穿越。允许反斜杠输入是为了兼容
    Windows 生成的路径字面量，但输出统一是正斜杠。
    """
    if raw_path is None:
        raise ValueError("path must not be empty")
    path = str(raw_path).strip()
    if not path:
        raise ValueError("path must not be empty")
    path = path.replace("\\", "/")
    if path.startswith("/"):
        raise ValueError("absolute paths are not allowed: " + path)
    # Windows drive letter, e.g. C:/...
    if len(path) >= 2 and path[1] == ":":
        raise ValueError("absolute paths are not allowed: " + path)
    parts = [segment for segment in path.split("/") if segment not in ("", ".")]
    for segment in parts:
        if segment == "..":
            raise ValueError("path traversal is not allowed: " + path)
    return "/".join(parts)


def resolve_workspace_relative_path(workspace_root, raw_path):
    """把相对路径拼进 workspace_root，并返回 Path 对象。"""
    normalized = normalize_workspace_relative_path(raw_path)
    root = Path(workspace_root).resolve()
    resolved = (root / normalized).resolve() if normalized else root
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("resolved path escapes workspace: " + normalized) from exc
    return resolved


def is_symlink(path):
    return os.path.islink(str(path))


def hash_file_bytes(path):
    """按原始字节读文件并算 sha256，不做换行归一化。"""
    file_path = Path(path)
    hasher = hashlib.sha256()
    size = 0
    with open(file_path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return {
        "hash_algorithm": "sha256",
        "content_hash": hasher.hexdigest(),
        "size_bytes": size,
    }


def hash_bytes(data):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("hash_bytes requires bytes-like input")
    hasher = hashlib.sha256()
    hasher.update(data)
    return {
        "hash_algorithm": "sha256",
        "content_hash": hasher.hexdigest(),
        "size_bytes": len(data),
    }
