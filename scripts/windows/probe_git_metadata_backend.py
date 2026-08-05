#!/usr/bin/env python3
"""Exercise Pony's anchored Git metadata reader on native Windows."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback


def _git(*args, cwd):
    subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def probe():
    root_path = Path(__file__).resolve().parents[2]
    if str(root_path) not in sys.path:
        sys.path.insert(0, str(root_path))

    from pony.tools.subprocess import (
        _lexical_git_repository_kind,
        discover_lexical_repo_root,
    )

    base = Path(tempfile.mkdtemp(prefix="pony-git-metadata-"))
    try:
        repo = base / "repo"
        repo.mkdir()
        _git("init", cwd=repo)
        _git("config", "user.name", "Pony Probe", cwd=repo)
        _git("config", "user.email", "pony@example.invalid", cwd=repo)
        (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        _git("add", "tracked.txt", cwd=repo)
        _git("commit", "-m", "probe", cwd=repo)
        nested = repo / "nested" / "deeper"
        nested.mkdir(parents=True)
        if discover_lexical_repo_root(nested) != repo:
            raise RuntimeError("repository root discovery mismatch")
        if _lexical_git_repository_kind(nested) != "directory":
            raise RuntimeError("directory repository classification mismatch")

        worktree = base / "worktree"
        _git("worktree", "add", str(worktree), cwd=repo)
        if discover_lexical_repo_root(worktree) != worktree:
            raise RuntimeError("linked worktree root discovery mismatch")
        if _lexical_git_repository_kind(worktree) != "linked-worktree":
            raise RuntimeError("linked worktree classification mismatch")

        marker = worktree / ".git"
        marker_data = marker.read_bytes()
        marker.unlink()
        outside = base / "outside.gitfile"
        outside.write_bytes(marker_data)
        os.link(outside, marker)
        try:
            _lexical_git_repository_kind(worktree)
        except ValueError as exc:
            if "unsafe git repository" not in str(exc):
                raise
        else:
            raise RuntimeError("hard-linked gitfile was accepted")

        bare = base / "bare.git"
        bare.mkdir()
        _git("init", "--bare", cwd=bare)
        if _lexical_git_repository_kind(bare) != "bare":
            raise RuntimeError("bare repository classification mismatch")

        return {
            "bare_repository": True,
            "hardlink_rejected": True,
            "linked_worktree": True,
            "nested_root_discovery": True,
            "schema_version": 1,
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    if os.name != "nt":
        print("windows_git_metadata_probe_requires_windows", file=sys.stderr)
        return 2
    try:
        result = probe()
    except BaseException as exc:
        traceback.print_exc()
        print(f"windows_git_metadata_probe_failed: {exc}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
