#!/usr/bin/env python3
"""Exercise Pony's production file-lock backend on native Windows."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def _load_backend():
    root_path = Path(__file__).resolve().parents[2]
    if str(root_path) not in sys.path:
        sys.path.insert(0, str(root_path))
    from pony.security.private_files import ensure_private_dir
    from pony.state.file_lock import locked_file

    return ensure_private_dir, locked_file


def _wait_for(path, process, deadline):
    while not path.exists():
        status = process.poll()
        if status is not None:
            raise RuntimeError(f"lock holder exited early with status {status}")
        if time.monotonic() >= deadline:
            raise TimeoutError("lock holder did not become ready")
        time.sleep(0.01)


def _hold(lock_path, ready, release):
    _ensure_private_dir, locked_file = _load_backend()
    with locked_file(lock_path, require_lock=True):
        ready.write_bytes(b"ready")
        while not release.exists():
            time.sleep(0.01)


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
    ensure_private_dir, locked_file = _load_backend()
    base = Path(tempfile.mkdtemp(prefix="pony-file-lock-"))
    try:
        root = ensure_private_dir(base / "state")
        lock_path = root / "state.lock"
        ready = root / "ready"
        release = root / "release"
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--hold",
                str(lock_path),
                "--ready",
                str(ready),
                "--release",
                str(release),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for(ready, process, time.monotonic() + 10)
            try:
                with locked_file(
                    lock_path,
                    require_lock=True,
                    require_existing=True,
                    lock_timeout=0.1,
                ):
                    raise RuntimeError("contended lock yielded")
            except TimeoutError:
                pass
            release.write_bytes(b"release")
            stdout, stderr = process.communicate(timeout=10)
            if process.returncode != 0:
                raise RuntimeError(
                    f"lock holder failed with status {process.returncode}: {stderr.strip()}"
                )
            if stdout.strip() or stderr.strip():
                raise RuntimeError("lock holder emitted unexpected output")
        finally:
            release.touch(exist_ok=True)
            if process.poll() is None:
                process.kill()
                process.communicate()

        with locked_file(
            lock_path,
            require_lock=True,
            require_existing=True,
            lock_timeout=1,
        ):
            try:
                with locked_file(lock_path, require_lock=True, lock_timeout=0):
                    raise RuntimeError("reentrant lock yielded")
            except RuntimeError as exc:
                if str(exc) != "lock reentry":
                    raise
            _expect_sharing_violation(
                lock_path.unlink,
                "locked file was deleted while its handle denied delete sharing",
            )
            _expect_sharing_violation(
                lambda: root.rename(base / "renamed"),
                "lock parent was renamed while its handle denied delete sharing",
            )

        missing_parent = base / "missing" / "missing.lock"
        try:
            with locked_file(missing_parent, require_existing=True):
                raise RuntimeError("missing required lock yielded")
        except (FileNotFoundError, OSError):
            pass
        if missing_parent.parent.exists():
            raise RuntimeError("require_existing created a missing lock parent")

        target = root / "target"
        target.write_bytes(b"target")
        hardlink = root / "hardlink.lock"
        os.link(target, hardlink)
        try:
            with locked_file(hardlink, require_lock=True):
                raise RuntimeError("hardlinked lock yielded")
        except ValueError as exc:
            if "multiple links" not in str(exc):
                raise

        return {
            "schema_version": 1,
            "cross_process_timeout": True,
            "reacquire_after_release": True,
            "same_thread_reentry_rejected": True,
            "require_existing_zero_write": True,
            "hardlink_rejected": True,
            "delete_share_denied": True,
            "parent_rename_denied": True,
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _parser():
    parser = argparse.ArgumentParser(
        description="Probe Pony's production LockFileEx backend."
    )
    parser.add_argument("--hold", type=Path)
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--release", type=Path)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("windows_file_lock_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        if args.hold is not None:
            if args.ready is None or args.release is None:
                raise ValueError("hold requires ready and release paths")
            _hold(args.hold, args.ready, args.release)
            return 0
        if args.ready is not None or args.release is not None:
            raise ValueError("ready/release are only valid with hold")
        result = probe()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"windows_file_lock_probe_failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
