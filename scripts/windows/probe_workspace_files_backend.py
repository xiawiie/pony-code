#!/usr/bin/env python3
"""Exercise Pony's production workspace-file backend on native Windows."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import traceback


def _expect_code(action, code):
    from pony.security.workspace_files import WorkspaceIOError

    try:
        action()
    except WorkspaceIOError as exc:
        if exc.code != code:
            raise
        return exc
    raise RuntimeError(f"workspace operation did not reject with {code}")


def _expect_sharing_violation(action, message):
    try:
        action()
    except PermissionError:
        return
    except OSError as exc:
        if getattr(exc, "winerror", None) in {5, 32}:
            return
        raise
    raise RuntimeError(message)


def probe():
    root_path = Path(__file__).resolve().parents[2]
    if str(root_path) not in sys.path:
        sys.path.insert(0, str(root_path))
    from pony.security import windows_native as native
    from pony.security.private_files import private_directory_identity
    from pony.security.workspace_files import (
        list_directory_names_anchored,
        read_regular_bytes_anchored,
        write_regular_bytes_anchored_atomic,
    )

    base = Path(tempfile.mkdtemp(prefix="pony-workspace-files-"))
    try:
        root = base / "workspace"
        root.mkdir()
        root_identity = private_directory_identity(root)

        created = write_regular_bytes_anchored_atomic(
            root,
            "nested/new.txt",
            b"old\n",
            max_bytes=1024,
            expected_root_identity=root_identity,
        )
        if not created["created"] or created["mode"] != stat.S_IFREG:
            raise RuntimeError("workspace atomic create metadata mismatch")
        opened = read_regular_bytes_anchored(
            root,
            "nested/new.txt",
            max_bytes=1024,
            expected_root_identity=root_identity,
        )
        if opened["data"] != b"old\n" or opened["sha256"] != hashlib.sha256(
            b"old\n"
        ).hexdigest():
            raise RuntimeError("workspace read content mismatch")

        listing = list_directory_names_anchored(
            root,
            ".",
            max_entries=16,
            expected_root_identity=root_identity,
        )
        listed = tuple((entry["name"], entry["mode"]) for entry in listing["entries"])
        if listed != (("nested", stat.S_IFDIR),) or listing["unsafe_count"]:
            raise RuntimeError("workspace directory listing mismatch")

        original_digest = opened["sha256"]
        replaced = write_regular_bytes_anchored_atomic(
            root,
            "nested/new.txt",
            b"new\n",
            max_bytes=1024,
            expected_sha256=original_digest,
            expected_root_identity=root_identity,
        )
        if replaced["created"] or (root / "nested" / "new.txt").read_bytes() != b"new\n":
            raise RuntimeError("workspace CAS replacement mismatch")

        target = root / "nested" / "new.txt"
        current_digest = hashlib.sha256(target.read_bytes()).hexdigest()

        def drift_target(_handle):
            target.write_bytes(b"drift\n")

        _expect_code(
            lambda: write_regular_bytes_anchored_atomic(
                root,
                "nested/new.txt",
                b"rejected\n",
                max_bytes=1024,
                expected_sha256=current_digest,
                expected_root_identity=root_identity,
                fsync_file=drift_target,
            ),
            "workspace_changed_during_write",
        )
        if target.read_bytes() != b"drift\n":
            raise RuntimeError("workspace CAS drift was overwritten")

        target.write_bytes(b"oversized")
        limited = _expect_code(
            lambda: read_regular_bytes_anchored(
                root,
                "nested/new.txt",
                max_bytes=4,
                expected_root_identity=root_identity,
            ),
            "workspace_file_limit_exceeded",
        )
        if not limited.state["exists"] or limited.state["mode"] != stat.S_IFREG:
            raise RuntimeError("workspace read limit state mismatch")

        linked = root / "linked.txt"
        linked.write_bytes(b"linked")
        hardlink = root / "hardlink.txt"
        os.link(linked, hardlink)
        _expect_code(
            lambda: read_regular_bytes_anchored(
                root,
                "hardlink.txt",
                max_bytes=1024,
                expected_root_identity=root_identity,
            ),
            "workspace_entry_unsafe",
        )
        _expect_code(
            lambda: write_regular_bytes_anchored_atomic(
                root,
                "hardlink.txt",
                b"rejected",
                max_bytes=1024,
                expected_root_identity=root_identity,
            ),
            "workspace_entry_unsafe",
        )

        renamed = base / "renamed-workspace"

        def prove_root_is_held(_handle):
            def rename_root():
                root.rename(renamed)
                renamed.rename(root)

            _expect_sharing_violation(
                rename_root,
                "workspace root was renamed during an atomic write",
            )

        write_regular_bytes_anchored_atomic(
            root,
            "held.txt",
            b"held",
            max_bytes=1024,
            expected_root_identity=root_identity,
            fsync_file=prove_root_is_held,
        )

        rollback = root / "rollback.txt"
        rollback.write_bytes(b"stable")
        rollback_digest = hashlib.sha256(b"stable").hexdigest()

        def reject_parent_sync(_handle):
            raise RuntimeError("reject parent sync")

        try:
            write_regular_bytes_anchored_atomic(
                root,
                "rollback.txt",
                b"rejected",
                max_bytes=1024,
                expected_sha256=rollback_digest,
                expected_root_identity=root_identity,
                fsync_parent=reject_parent_sync,
            )
        except RuntimeError as exc:
            if str(exc) != "reject parent sync":
                raise
        else:
            raise RuntimeError("workspace parent sync failure was ignored")
        if rollback.read_bytes() != b"stable":
            raise RuntimeError("workspace replacement rollback content mismatch")

        real_rename = native.rename_handle
        failed_rename = False

        def ambiguous_rename(handle, parent, name, **kwargs):
            nonlocal failed_rename
            result = real_rename(handle, parent, name, **kwargs)
            if not failed_rename and name == "ambiguous.txt":
                failed_rename = True
                raise OSError(5, "simulated handle rename result ambiguity")
            return result

        native.rename_handle = ambiguous_rename
        try:
            try:
                write_regular_bytes_anchored_atomic(
                    root,
                    "ambiguous.txt",
                    b"ambiguous",
                    max_bytes=1024,
                    expected_root_identity=root_identity,
                )
            except OSError:
                pass
            else:
                raise RuntimeError("ambiguous workspace create did not fail")
        finally:
            native.rename_handle = real_rename
        if (root / "ambiguous.txt").exists():
            raise RuntimeError("ambiguous workspace create was not rolled back")

        long_relative = "/".join(
            [*(f"component-{index}-" + "x" * 30 for index in range(7)), "artifact.txt"]
        )
        write_regular_bytes_anchored_atomic(
            root,
            long_relative,
            b"long",
            max_bytes=1024,
            expected_root_identity=root_identity,
        )
        if read_regular_bytes_anchored(
            root,
            long_relative,
            max_bytes=1024,
            expected_root_identity=root_identity,
        )["data"] != b"long":
            raise RuntimeError("workspace long-path round trip failed")

        artifacts = [
            path
            for path in root.rglob("*")
            if path.suffix in {".tmp", ".bak"}
        ]
        if artifacts:
            raise RuntimeError("workspace atomic write left cleanup artifacts")
        return {
            "schema_version": 1,
            "anchored_create": True,
            "bounded_read": True,
            "directory_listing": True,
            "cas_replace": True,
            "drift_rejected": True,
            "hardlink_rejected": True,
            "root_rename_denied": True,
            "rollback": True,
            "commit_ambiguity_rollback": True,
            "long_path": True,
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main(argv=None):
    argparse.ArgumentParser(
        description="Probe Pony's production Windows workspace-file backend."
    ).parse_args(argv)
    if os.name != "nt":
        print("windows_workspace_files_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        result = probe()
    except (OSError, RuntimeError, ValueError) as exc:
        traceback.print_exc()
        print(f"windows_workspace_files_probe_failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
