#!/usr/bin/env python3
"""Exercise Pony's production Memory diagnostics on native Windows."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import traceback


def probe():
    root_path = Path(__file__).resolve().parents[2]
    if str(root_path) not in sys.path:
        sys.path.insert(0, str(root_path))

    from pony.memory.diagnostics import collect_memory_diagnostics
    from pony.security.private_files import (
        ensure_private_dir,
        private_directory_identity,
        write_private_bytes_atomic,
    )

    base = Path(tempfile.mkdtemp(prefix="pony-memory-"))
    try:
        repo = base / "repo"
        memory = ensure_private_dir(repo / ".pony" / "memory")
        notes = ensure_private_dir(memory / "notes" / "nested")
        (notes / "note.md").write_text("healthy note\n", encoding="utf-8")
        identity = private_directory_identity(memory)
        write_private_bytes_atomic(
            memory / "agent_notes.md",
            b"healthy agent note\n",
            trusted_root=memory,
            trusted_root_identity=identity,
        )
        missing_user = base / "missing-user" / ".pony" / "memory"
        healthy = collect_memory_diagnostics(repo, user_memory_root=missing_user)
        if healthy["status"] != "pass" or healthy["issues"]:
            raise RuntimeError(f"healthy Memory diagnostics failed: {healthy!r}")
        if missing_user.exists():
            raise RuntimeError("Memory diagnostics created the missing user root")

        canary = "memory-diagnostics-secret-canary"
        outside = base / "outside.md"
        outside.write_text(canary, encoding="utf-8")
        os.link(outside, memory / "notes" / "hardlink.md")
        unsafe = collect_memory_diagnostics(repo, user_memory_root=missing_user)
        if unsafe["status"] != "unknown" or not any(
            issue["reason_code"] == "memory_file_unavailable"
            for issue in unsafe["issues"]
        ):
            raise RuntimeError(f"hardlink Memory diagnostics mismatch: {unsafe!r}")
        if canary in json.dumps(unsafe) or missing_user.exists():
            raise RuntimeError("Memory diagnostics leaked content or created state")

        return {
            "hardlink_rejected": True,
            "healthy_scan": True,
            "missing_root_unchanged": True,
            "schema_version": 1,
            "secret_not_projected": True,
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    if os.name != "nt":
        print("windows_memory_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        result = probe()
    except BaseException as exc:
        traceback.print_exc()
        print(f"windows_memory_probe_failed: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
