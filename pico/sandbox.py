"""Platform-neutral contracts for an approved sandbox execution."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Mapping, Protocol, Sequence

_MAX_BUNDLE_MANIFEST_BYTES = 16 * 1024 * 1024
TARGET_START_ENV = "PICO_INTERNAL_TARGET_START_TOKEN"
_TARGET_START_PREFIX = "\x1ePICO_TARGET_STARTED:"
_TARGET_START_SUFFIX = "\x1e\n"


TARGET_START_WRAPPER = """
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const [file, ...args] = process.argv.slice(1);
const token = process.env.PICO_INTERNAL_TARGET_START_TOKEN || '';
const childEnv = {...process.env};
delete childEnv.PICO_INTERNAL_TARGET_START_TOKEN;
if (!/^[0-9a-f]{64}$/.test(token)) process.exit(125);
const child = spawn(file, args, {stdio: 'inherit', env: childEnv});
child.once('spawn', () => {
  try {
    fs.writeSync(2, '\\x1ePICO_TARGET_STARTED:' + token + '\\x1e\\n');
  } catch {
    child.kill('SIGKILL');
    process.exit(125);
  }
});
child.once('error', () => process.exit(126));
for (const name of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(name, () => child.kill(name));
}
child.once('exit', code => process.exit(code ?? 1));
""".strip()


def new_target_start_token() -> str:
    return secrets.token_hex(32)


def target_start_frame(token: str) -> str:
    return f"{_TARGET_START_PREFIX}{token}{_TARGET_START_SUFFIX}"


def consume_target_start_frame(stderr: str, token: str) -> tuple[str, bool]:
    frame = target_start_frame(token)
    if frame not in stderr:
        return stderr, False
    return stderr.replace(frame, "", 1), True


@dataclass(frozen=True)
class FileIdentity:
    relative_path: str
    owner: int
    mode: int
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


def capture_file_identity(path: Path, root: Path) -> FileIdentity:
    path = Path(path)
    root = Path(root).resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or path.is_symlink() or not path.is_file():
        raise ValueError("sandbox identity path is unsafe")
    metadata = path.stat()
    return FileIdentity(
        relative_path=resolved.relative_to(root).as_posix(),
        owner=metadata.st_uid,
        mode=stat.S_IMODE(metadata.st_mode),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _read_bundle_manifest(root: Path) -> bytes:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    root_descriptor = None
    descriptor = None
    try:
        root_descriptor = os.open(root, directory_flags)
        before = os.stat(
            ".pico-toolchain.json",
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        descriptor = os.open(
            ".pico-toolchain.json", file_flags, dir_fd=root_descriptor
        )
        opened = os.fstat(descriptor)
        after_open = os.stat(
            ".pico-toolchain.json",
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )

        def identity(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) & 0o077
            or opened.st_size > _MAX_BUNDLE_MANIFEST_BYTES
            or identity(before) != identity(opened)
            or identity(after_open) != identity(opened)
        ):
            raise ValueError("sandbox bundle manifest changed")
        chunks = []
        remaining = _MAX_BUNDLE_MANIFEST_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after_read = os.fstat(descriptor)
        if (
            len(data) > _MAX_BUNDLE_MANIFEST_BYTES
            or len(data) != opened.st_size
            or identity(after_read) != identity(opened)
        ):
            raise ValueError("sandbox bundle manifest changed")
        return data
    except OSError as exc:
        raise ValueError("sandbox bundle manifest changed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)


@dataclass(frozen=True)
class SandboxIdentity:
    trusted_root: Path
    node_path: Path
    srt_entry_path: Path
    package_json_path: Path | None = None
    bundle_manifest_hash: str = ""
    file_identities: tuple[FileIdentity, ...] = ()

    def verify(self) -> None:
        root = Path(self.trusted_root).resolve(strict=True)
        metadata = root.stat()
        if self.bundle_manifest_hash and (
            metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("sandbox root identity changed")
        identities = self.file_identities
        if not identities:
            # Legacy test/adapter contexts can still bind the two executable
            # files; production toolchains always provide a complete manifest.
            identities = tuple(
                capture_file_identity(path, root)
                for path in (self.node_path, self.srt_entry_path)
            )
        for expected in identities:
            actual = capture_file_identity(root / expected.relative_path, root)
            if actual != expected:
                raise ValueError("sandbox file identity changed")
        if self.bundle_manifest_hash:
            marker_bytes = _read_bundle_manifest(root)
            if hashlib.sha256(marker_bytes).hexdigest() != self.bundle_manifest_hash:
                raise ValueError("sandbox bundle manifest changed")
            try:
                expected_tree = json.loads(marker_bytes.decode("utf-8"))["tree"]
            except (UnicodeError, ValueError, KeyError, TypeError) as exc:
                raise ValueError("sandbox bundle manifest changed") from exc
            actual_tree = {}
            for path in sorted(root.rglob("*")):
                if path.name == ".pico-toolchain.json" or path.is_dir():
                    continue
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    target = os.readlink(path)
                    resolved = (path.parent / target).resolve(strict=False)
                    if not resolved.is_relative_to(root):
                        raise ValueError("sandbox tree identity changed")
                    payload = f"symlink:{target}".encode()
                elif path.is_file():
                    payload = path.read_bytes()
                else:
                    raise ValueError("sandbox tree identity changed")
                actual_tree[relative] = hashlib.sha256(payload).hexdigest()
            if actual_tree != expected_tree:
                raise ValueError("sandbox tree identity changed")


@dataclass(frozen=True)
class SandboxContext:
    identity: SandboxIdentity
    workspace_root: Path
    original_home: Path
    policy_hash: str = ""


class ApprovedExecution(Protocol):
    argv: Sequence[str]
    cwd: Path
    env: Mapping[str, str]
    timeout: float


@dataclass(frozen=True)
class SandboxOutcome:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    target_started: bool
    wrapper_status: str
    sandbox_outcome: str
    cleanup_status: str
    residue_detected: bool = False
