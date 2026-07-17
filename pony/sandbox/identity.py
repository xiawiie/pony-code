"""Canonical identity checks for the local Docker Sandbox runtime."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


MAX_RECORD_BYTES = 256 * 1024
MAX_INSTALLED_TREE_BYTES = 512 * 1024 * 1024
MAX_INSTALLED_FILE_BYTES = 256 * 1024 * 1024
MAX_INSTALLED_TREE_ENTRIES = 10_000
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
_DIST_INFO_FILES = ("METADATA", "WHEEL", "entry_points.txt", "top_level.txt")


class SandboxIdentityError(RuntimeError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


def canonical_json(value):
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise SandboxIdentityError("sandbox_identity_invalid") from exc


def canonical_digest(value):
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def _entry_identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _ignored_installed_path(relative):
    return relative.parent.name == "__pycache__" and relative.suffix in {
        ".pyc",
        ".pyo",
    }


def _installed_inventory(root):
    entries = []
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _ignored_installed_path(relative):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        ):
            raise SandboxIdentityError("installed_distribution_invalid")
        entries.append((relative.as_posix(), _entry_identity(info)))
        if len(entries) > MAX_INSTALLED_TREE_ENTRIES:
            raise SandboxIdentityError("installed_distribution_invalid")
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1 or info.st_size > MAX_INSTALLED_FILE_BYTES:
                raise SandboxIdentityError("installed_distribution_invalid")
            total += info.st_size
            if total > MAX_INSTALLED_TREE_BYTES:
                raise SandboxIdentityError("installed_distribution_invalid")
    return entries


def _installed_file_digest(path, expected_identity):
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if (
            _entry_identity(before) != expected_identity
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            raise SandboxIdentityError("installed_distribution_invalid")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise SandboxIdentityError("installed_distribution_invalid")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise SandboxIdentityError("installed_distribution_invalid")
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            _entry_identity(after) != expected_identity
            or _entry_identity(current) != expected_identity
        ):
            raise SandboxIdentityError("installed_distribution_invalid")
        return "sha256:" + digest.hexdigest()
    except SandboxIdentityError:
        raise
    except OSError as exc:
        raise SandboxIdentityError("installed_distribution_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _installed_record_rows(raw):
    try:
        rows = {}
        for row in csv.reader(io.StringIO(raw.decode("utf-8"), newline="")):
            if len(row) != 3:
                raise ValueError("invalid RECORD row")
            raw_path = row[0]
            parts = raw_path.split("/")
            external_prefix = next(
                (index for index, part in enumerate(parts) if part != ".."),
                len(parts),
            )
            if (
                "\\" in raw_path
                or any(
                    ord(character) < 0x20 or ord(character) == 0x7F
                    for character in raw_path
                )
                or external_prefix == len(parts)
                or any(part in {"", "."} for part in parts)
                or any(part == ".." for part in parts[external_prefix:])
            ):
                raise ValueError("invalid RECORD path")
            normalized = PurePosixPath(*parts).as_posix()
            if normalized in rows:
                raise ValueError("duplicate RECORD path")
            rows[normalized] = (row[1], row[2])
        return rows
    except (UnicodeDecodeError, csv.Error, ValueError) as exc:
        raise SandboxIdentityError("installed_distribution_invalid") from exc


def _installed_record_bytes(path, identity):
    if identity[6] > MAX_RECORD_BYTES:
        raise SandboxIdentityError("installed_distribution_invalid")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if _entry_identity(before) != identity or not stat.S_ISREG(before.st_mode):
            raise SandboxIdentityError("installed_distribution_invalid")
        remaining = before.st_size
        chunks = []
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise SandboxIdentityError("installed_distribution_invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise SandboxIdentityError("installed_distribution_invalid")
        after = os.fstat(descriptor)
        current = path.lstat()
        if _entry_identity(after) != identity or _entry_identity(current) != identity:
            raise SandboxIdentityError("installed_distribution_invalid")
        return b"".join(chunks)
    except SandboxIdentityError:
        raise
    except OSError as exc:
        raise SandboxIdentityError("installed_distribution_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _record_digest(file_digest):
    raw = bytes.fromhex(file_digest.removeprefix("sha256:"))
    return "sha256=" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def installed_tree_digest(package_root, distribution_version=None):
    root = Path(os.path.abspath(os.fspath(package_root)))
    try:
        root_before = root.lstat()
    except OSError as exc:
        raise SandboxIdentityError("installed_distribution_invalid") from exc
    if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
        raise SandboxIdentityError("installed_distribution_invalid")
    first = _installed_inventory(root)
    rendered = []
    for relative, identity in first:
        if stat.S_ISDIR(identity[2]):
            continue
        path = root / Path(relative)
        rendered.append(
            {
                "path": relative,
                "mode": stat.S_IMODE(identity[2]),
                "size": identity[6],
                "sha256": _installed_file_digest(path, identity),
            }
        )
    try:
        root_after = root.lstat()
    except OSError as exc:
        raise SandboxIdentityError("installed_distribution_invalid") from exc
    if (
        _entry_identity(root_before) != _entry_identity(root_after)
        or first != _installed_inventory(root)
    ):
        raise SandboxIdentityError("installed_distribution_invalid")
    if distribution_version is None:
        return canonical_digest(rendered)
    if (
        root.name != "pony"
        or not isinstance(distribution_version, str)
        or _VERSION_RE.fullmatch(distribution_version) is None
    ):
        raise SandboxIdentityError("installed_distribution_invalid")
    dist_info = root.parent / f"pony_code-{distribution_version}.dist-info"
    try:
        dist_before = dist_info.lstat()
    except OSError as exc:
        raise SandboxIdentityError("installed_distribution_invalid") from exc
    if stat.S_ISLNK(dist_before.st_mode) or not stat.S_ISDIR(dist_before.st_mode):
        raise SandboxIdentityError("installed_distribution_invalid")
    identities = {}
    for name in (*_DIST_INFO_FILES, "RECORD"):
        try:
            info = (dist_info / name).lstat()
        except OSError as exc:
            raise SandboxIdentityError("installed_distribution_invalid") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > MAX_INSTALLED_FILE_BYTES
        ):
            raise SandboxIdentityError("installed_distribution_invalid")
        identities[name] = _entry_identity(info)
    record_raw = _installed_record_bytes(dist_info / "RECORD", identities["RECORD"])
    records = _installed_record_rows(record_raw)
    package_records = {
        f"pony/{relative}"
        for relative, identity in first
        if stat.S_ISREG(identity[2])
    }
    ignored_package_records = {
        name
        for name, fields in records.items()
        if name.startswith("pony/")
        and _ignored_installed_path(PurePosixPath(name).relative_to("pony"))
        and fields == ("", "")
    }
    dist_records = {f"{dist_info.name}/{name}" for name in _DIST_INFO_FILES}
    record_path = f"{dist_info.name}/RECORD"
    if (
        {name for name in records if name.startswith("pony/")}
        != package_records | ignored_package_records
        or not dist_records <= set(records)
        or records.get(record_path) != ("", "")
    ):
        raise SandboxIdentityError("installed_distribution_invalid")
    distribution_files = []
    for name in _DIST_INFO_FILES:
        path = dist_info / name
        identity = identities[name]
        digest = _installed_file_digest(path, identity)
        record_digest, record_size = records[f"{dist_info.name}/{name}"]
        if record_digest != _record_digest(digest) or record_size != str(identity[6]):
            raise SandboxIdentityError("installed_distribution_invalid")
        distribution_files.append(
            {
                "path": f"{dist_info.name}/{name}",
                "mode": stat.S_IMODE(identity[2]),
                "size": identity[6],
                "sha256": digest,
            }
        )
    for item in rendered:
        item["path"] = "pony/" + item["path"]
        record_digest, record_size = records[item["path"]]
        if record_digest != _record_digest(item["sha256"]) or record_size != str(
            item["size"]
        ):
            raise SandboxIdentityError("installed_distribution_invalid")
    try:
        dist_after = dist_info.lstat()
    except OSError as exc:
        raise SandboxIdentityError("installed_distribution_invalid") from exc
    if (
        _entry_identity(dist_before) != _entry_identity(dist_after)
        or any(
            _entry_identity((dist_info / name).lstat()) != identity
            for name, identity in identities.items()
        )
    ):
        raise SandboxIdentityError("installed_distribution_invalid")
    return canonical_digest(
        [
            {"distribution": "pony", "version": distribution_version},
            *rendered,
            *distribution_files,
        ]
    )
