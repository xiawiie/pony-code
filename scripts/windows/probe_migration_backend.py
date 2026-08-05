#!/usr/bin/env python3
"""Exercise Pony's production migration backend on native Windows."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import traceback


def _migration(root, *, name="runs", validate=lambda path: True):
    from pony.state.migration import Migration

    return Migration(
        root,
        contract="windows_probe",
        source_version=1,
        target_version=2,
        live=name,
        workspace_identity={"repo_commit": "probe", "repo_dirty": False},
        validate=validate,
    )


def _builder(source, candidate):
    shutil.copytree(source, candidate)
    (candidate / "value.txt").write_text("new", encoding="utf-8")
    nested = candidate / "nested"
    nested.mkdir()
    (nested / "note.txt").write_text("nested", encoding="utf-8")


def _new_root(base, name):
    from pony.security.private_files import ensure_private_dir

    root = ensure_private_dir(base / name / ".pony")
    live = root / "runs"
    live.mkdir()
    (live / "value.txt").write_text("old", encoding="utf-8")
    return root


def probe():
    root_path = Path(__file__).resolve().parents[2]
    if str(root_path) not in sys.path:
        sys.path.insert(0, str(root_path))

    from pony.state.migration import ABSENT, OLD_MOVED, ROLLED_BACK

    base = Path(tempfile.mkdtemp(prefix="pony-migration-"))
    try:
        committed = _migration(_new_root(base, "commit"))
        if committed.apply(_builder) != ABSENT:
            raise RuntimeError("migration commit did not finish")
        if (committed.live / "nested" / "note.txt").read_text(encoding="utf-8") != "nested":
            raise RuntimeError("nested migration content mismatch")

        rolled_back = _migration(_new_root(base, "rollback"), validate=lambda path: False)
        if rolled_back.apply(_builder) != ROLLED_BACK:
            raise RuntimeError("migration validation failure did not roll back")
        if (rolled_back.live / "value.txt").read_text(encoding="utf-8") != "old":
            raise RuntimeError("migration rollback content mismatch")
        if rolled_back.recover() != ABSENT:
            raise RuntimeError("rolled-back migration cleanup did not finish")

        recovered = _migration(_new_root(base, "recover"))
        original_write = recovered._write

        def stop_after_old_move(value, state, error=""):
            result = original_write(value, state, error)
            if state == OLD_MOVED:
                raise KeyboardInterrupt
            return result

        recovered._write = stop_after_old_move
        try:
            recovered.apply(_builder)
        except KeyboardInterrupt:
            pass
        else:
            raise RuntimeError("migration crash injection did not run")
        recovered._write = original_write
        if recovered.recover() != ABSENT:
            raise RuntimeError("migration recovery did not finish")
        if (recovered.live / "value.txt").read_text(encoding="utf-8") != "new":
            raise RuntimeError("recovered migration content mismatch")

        unsafe = _migration(_new_root(base, "hardlink"))

        def hardlink_builder(source, candidate):
            shutil.copytree(source, candidate)
            os.link(candidate / "value.txt", candidate / "linked.txt")

        try:
            unsafe.apply(hardlink_builder)
        except ValueError as exc:
            if "multiple links" not in str(exc):
                raise
        else:
            raise RuntimeError("migration accepted a hard-linked candidate")

        return {
            "commit": True,
            "crash_recovery": True,
            "hardlink_rejected": True,
            "nested_manifest": True,
            "rollback": True,
            "schema_version": 1,
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    if os.name != "nt":
        print("windows_migration_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        result = probe()
    except BaseException as exc:
        traceback.print_exc()
        print(f"windows_migration_probe_failed: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
