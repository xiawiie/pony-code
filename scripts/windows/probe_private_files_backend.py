#!/usr/bin/env python3
"""Exercise Pony's production private-file backend on native Windows."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

def probe():
    root_path = Path(__file__).resolve().parents[2]
    if str(root_path) not in sys.path:
        sys.path.insert(0, str(root_path))
    from pony.security.private_files import (
        append_private_bytes,
        ensure_private_dir,
        harden_private_tree,
        private_directory_identity,
        private_file_signature,
        read_private_bytes,
        write_private_bytes_atomic,
    )

    base = Path(tempfile.mkdtemp(prefix="pony-private-files-"))
    try:
        root = ensure_private_dir(base / "state")
        root_identity = private_directory_identity(root)
        target = root / "session.jsonl"
        write_private_bytes_atomic(
            target,
            b"old\n",
            trusted_root=root,
            trusted_root_identity=root_identity,
            require_absent=True,
        )
        original = private_file_signature(
            target,
            trusted_root=root,
            trusted_root_identity=root_identity,
        )
        append_private_bytes(
            target,
            b"trace\n",
            trusted_root=root,
            trusted_root_identity=root_identity,
            expected_identity=(original.filesystem_id, original.file_id),
        )
        if read_private_bytes(
            target,
            trusted_root=root,
            trusted_root_identity=root_identity,
        ) != b"old\ntrace\n":
            raise RuntimeError("private append content mismatch")

        calls = 0

        def reject_after_install():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("reject installed state")

        try:
            write_private_bytes_atomic(
                target,
                b"rejected\n",
                trusted_root=root,
                trusted_root_identity=root_identity,
                validate_commit=reject_after_install,
            )
        except RuntimeError as exc:
            if str(exc) != "reject installed state":
                raise
        else:
            raise RuntimeError("post-install validation failure was ignored")
        if read_private_bytes(
            target,
            trusted_root=root,
            trusted_root_identity=root_identity,
        ) != b"old\ntrace\n":
            raise RuntimeError("private atomic rollback content mismatch")

        write_private_bytes_atomic(
            target,
            b"new\n",
            trusted_root=root,
            trusted_root_identity=root_identity,
        )
        final = private_file_signature(
            target,
            trusted_root=root,
            trusted_root_identity=root_identity,
        )
        if not final.is_private or final.file_id == original.file_id:
            raise RuntimeError("private atomic replacement identity mismatch")
        if read_private_bytes(
            target,
            trusted_root=root,
            trusted_root_identity=root_identity,
            max_bytes=4,
        ) != b"new\n":
            raise RuntimeError("private atomic replacement content mismatch")

        nested = root / "nested"
        nested.mkdir()
        (nested / "note.txt").write_bytes(b"note")
        long_root = root.joinpath(*(f"component-{index}-" + "x" * 30 for index in range(7)))
        ensure_private_dir(long_root)
        long_identity = private_directory_identity(long_root)
        write_private_bytes_atomic(
            long_root / "artifact.json",
            b"{}",
            trusted_root=long_root,
            trusted_root_identity=long_identity,
        )
        harden_private_tree(root)
        nested_signature = private_file_signature(
            nested / "note.txt",
            trusted_root=root,
            trusted_root_identity=root_identity,
        )
        if not nested_signature.is_private:
            raise RuntimeError("private tree hardening failed")
        if any(path.suffix in {".tmp", ".bak"} for path in root.iterdir()):
            raise RuntimeError("private atomic write left cleanup artifacts")
        return {
            "schema_version": 1,
            "root_identity": "volume-file-id",
            "atomic_create": True,
            "atomic_replace": True,
            "atomic_rollback": True,
            "append": True,
            "private_dacl": True,
            "tree_hardening": True,
            "long_path": True,
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    if os.name != "nt":
        print("windows_private_files_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        result = probe()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"windows_private_files_probe_failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
