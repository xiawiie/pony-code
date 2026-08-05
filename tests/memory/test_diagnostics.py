import json
import os
import stat
from pathlib import Path

import pytest

import pony.memory.block_store as block_store_module
import pony.memory.diagnostics as diagnostics_module
from pony.cli.app import main
from pony.cli.diagnostics import collect_doctor
from pony.memory.diagnostics import collect_memory_diagnostics


def _memory_root(repo):
    root = repo / ".pony" / "memory"
    (root / "notes").mkdir(parents=True)
    return root


def test_windows_memory_health_uses_anchored_backends(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    identities = {
        ".": (1, "root"),
        "notes": (1, "notes"),
        "notes/nested": (1, "nested"),
    }
    listings = {
        ".": (
            {"name": "notes", "mode": stat.S_IFDIR, "identity": identities["notes"]},
            {"name": "agent_notes.md", "mode": stat.S_IFREG, "identity": (1, "agent")},
        ),
        "notes": (
            {"name": "note.md", "mode": stat.S_IFREG, "identity": (1, "note")},
            {
                "name": "nested",
                "mode": stat.S_IFDIR,
                "identity": identities["notes/nested"],
            },
        ),
        "notes/nested": (
            {"name": "deep.md", "mode": stat.S_IFREG, "identity": (1, "deep")},
        ),
    }
    file_identities = {
        "agent_notes.md": (1, "agent"),
        "notes/note.md": (1, "note"),
        "notes/nested/deep.md": (1, "deep"),
    }
    reads = []

    monkeypatch.setattr(
        diagnostics_module.private_files,
        "private_directory_identity",
        lambda _path: identities["."],
    )

    def list_directory(_root, relative, **_kwargs):
        return {
            "entries": listings[relative],
            "unsafe_count": 0,
            "scanned": len(listings[relative]),
            "identity": identities[relative],
        }

    def read_file(_root, relative, **_kwargs):
        reads.append(relative)
        return {
            "exists": True,
            "identity": file_identities[relative],
            "data": b"safe",
        }

    monkeypatch.setattr(
        diagnostics_module.workspace_files,
        "list_directory_names_anchored",
        list_directory,
    )
    monkeypatch.setattr(
        diagnostics_module.workspace_files,
        "read_regular_bytes_anchored",
        read_file,
    )

    issues = []
    diagnostics_module._scan_scope_windows(
        "workspace", root, issues, {"entries": 0, "bytes": 0}
    )

    assert issues == []
    assert reads == ["notes/note.md", "notes/nested/deep.md", "agent_notes.md"]


def test_windows_memory_health_reports_unsafe_root_entries(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    monkeypatch.setattr(
        diagnostics_module.private_files,
        "private_directory_identity",
        lambda _path: (1, "root"),
    )
    monkeypatch.setattr(
        diagnostics_module.workspace_files,
        "list_directory_names_anchored",
        lambda *_args, **_kwargs: {
            "entries": (),
            "unsafe_count": 1,
            "scanned": 1,
            "identity": (1, "root"),
        },
    )

    issues = []
    state = {"entries": 0, "bytes": 0}
    diagnostics_module._scan_scope_windows("workspace", root, issues, state)

    assert issues == [
        {
            "path": "workspace",
            "count": 1,
            "reason_code": "memory_directory_unavailable",
            "limit": 0,
        }
    ]
    assert state == {"entries": 1, "bytes": 0}


def test_memory_health_is_bounded_and_does_not_validate_note_content(tmp_path):
    repo = tmp_path / "repo"
    memory = _memory_root(repo)
    (memory / "notes" / "note.md").write_text(
        "---\nname without a colon\n---\nbody\n", encoding="utf-8"
    )
    (repo / ".gitignore").write_text(".pony/\n", encoding="utf-8")

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user" / ".pony" / "memory",
    )

    assert result == {
        "check_id": "memory",
        "status": "pass",
        "reason_code": "memory_diagnostics_passed",
        "remediation": "",
        "issues": [],
    }


@pytest.mark.parametrize("unsafe_target", ("file", "notes_directory"))
def test_memory_health_reports_unsafe_entries_without_leaking_content(
    tmp_path, unsafe_target
):
    canary = "memory-health-secret-canary"
    repo = tmp_path / "repo"
    memory = _memory_root(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(canary, encoding="utf-8")
    if unsafe_target == "file":
        (memory / "notes" / "linked.md").symlink_to(outside / "secret.md")
    else:
        (memory / "notes").rmdir()
        (memory / "notes").symlink_to(outside, target_is_directory=True)
    (memory / "agent_notes.md").write_text(canary, encoding="utf-8")
    if hasattr(os, "link"):
        hardlink = memory / "notes" / "hardlink.md"
        if unsafe_target == "file":
            os.link(outside / "secret.md", hardlink)

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user" / ".pony" / "memory",
    )
    serialized = json.dumps(result)

    assert result["status"] == "unknown"
    assert result["reason_code"] == "memory_diagnostics_incomplete"
    assert canary not in serialized
    assert str(outside) not in serialized
    assert {issue["reason_code"] for issue in result["issues"]} >= {
        "memory_directory_unavailable"
        if unsafe_target == "notes_directory"
        else "memory_file_unavailable"
    }


def test_memory_health_reports_bounded_file_count(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    memory = _memory_root(repo)
    (memory / "notes" / "a.md").write_text("a", encoding="utf-8")
    (memory / "notes" / "b.md").write_text("b", encoding="utf-8")
    monkeypatch.setattr(block_store_module, "MAX_MEMORY_INDEX_FILES", 1)

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user" / ".pony" / "memory",
    )

    assert result["status"] == "unknown"
    assert result["issues"] == [
        {
            "path": "workspace/notes/b.md",
            "count": 2,
            "reason_code": "memory_index_limit_reached",
            "limit": 1,
        }
    ]


def test_memory_health_does_not_create_missing_roots(tmp_path):
    repo = tmp_path / "repo"
    user = tmp_path / "home" / ".pony" / "memory"

    result = collect_memory_diagnostics(repo, user_memory_root=user)

    assert result["status"] == "pass"
    assert not (repo / ".pony" / "memory").exists()
    assert not user.exists()


@pytest.mark.skipif(not hasattr(os, "chmod"), reason="POSIX mode assertion")
def test_memory_health_does_not_change_file_mode(tmp_path):
    repo = tmp_path / "repo"
    memory = _memory_root(repo)
    note = memory / "agent_notes.md"
    note.write_text("agent note", encoding="utf-8")
    note.chmod(0o644)
    before = note.stat().st_mode

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user" / ".pony" / "memory",
    )

    assert result["status"] == "pass"
    assert note.stat().st_mode == before


def test_memory_health_fails_closed_when_root_is_replaced_during_scan(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    memory = _memory_root(repo)
    (memory / "notes" / "old.md").write_text("old note", encoding="utf-8")
    (memory / "agent_notes.md").write_text("old agent", encoding="utf-8")
    old_root_identity = diagnostics_module.private_files.private_directory_identity(
        memory
    )
    old_notes_identity = diagnostics_module.private_files.private_directory_identity(
        memory / "notes"
    )

    replacement = tmp_path / "replacement"
    (replacement / "notes").mkdir(parents=True)
    (replacement / "notes" / "new.md").write_text("new note", encoding="utf-8")
    (replacement / "agent_notes.md").write_text("new agent", encoding="utf-8")
    displaced = tmp_path / "displaced"
    replaced = False
    parent_identities = []

    if os.name == "nt":
        original_read = diagnostics_module.workspace_files.read_regular_bytes_anchored

        def replace_root(root, relative, **kwargs):
            nonlocal replaced
            if not replaced:
                memory.rename(displaced)
                replacement.rename(memory)
                replaced = True
            return original_read(root, relative, **kwargs)

        monkeypatch.setattr(
            diagnostics_module.workspace_files,
            "read_regular_bytes_anchored",
            replace_root,
        )
    else:
        original_read = diagnostics_module._read_bounded_at

        def replace_root(parent_descriptor, name, expected, limit):
            nonlocal replaced
            if not replaced:
                memory.rename(displaced)
                replacement.rename(memory)
                replaced = True
            parent_identities.append(
                diagnostics_module._identity(os.fstat(parent_descriptor))
            )
            return original_read(parent_descriptor, name, expected, limit)

        monkeypatch.setattr(diagnostics_module, "_read_bounded_at", replace_root)

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user" / ".pony" / "memory",
    )

    assert replaced
    if os.name != "nt":
        assert parent_identities == [old_notes_identity, old_root_identity]
    assert result["status"] == "unknown"
    assert {
        "path": "workspace",
        "count": 1,
        "reason_code": "memory_root_changed",
        "limit": 0,
    } in result["issues"]


def test_memory_health_fails_closed_when_nested_directory_is_replaced(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    memory = _memory_root(repo)
    nested = memory / "notes" / "nested"
    nested.mkdir()
    (nested / "old.md").write_text("old note", encoding="utf-8")
    replacement = tmp_path / "replacement-nested"
    replacement.mkdir()
    (replacement / "new.md").write_text("new note", encoding="utf-8")
    displaced = tmp_path / "displaced-nested"
    replaced = False

    if os.name == "nt":
        original_read = diagnostics_module.workspace_files.read_regular_bytes_anchored

        def replace_nested(root, relative, **kwargs):
            nonlocal replaced
            if not replaced:
                nested.rename(displaced)
                replacement.rename(nested)
                replaced = True
            return original_read(root, relative, **kwargs)

        monkeypatch.setattr(
            diagnostics_module.workspace_files,
            "read_regular_bytes_anchored",
            replace_nested,
        )
    else:
        original_read = diagnostics_module._read_bounded_at

        def replace_nested(parent_descriptor, name, expected, limit):
            nonlocal replaced
            if not replaced:
                nested.rename(displaced)
                replacement.rename(nested)
                replaced = True
            return original_read(parent_descriptor, name, expected, limit)

        monkeypatch.setattr(diagnostics_module, "_read_bounded_at", replace_nested)

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user" / ".pony" / "memory",
    )

    assert replaced
    assert result["status"] == "unknown"
    assert {
        "path": "workspace/notes/nested",
        "count": 1,
        "reason_code": "memory_directory_unavailable",
        "limit": 0,
    } in result["issues"]


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlink unavailable")
def test_memory_health_fails_closed_when_hardlink_is_added_during_read(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    memory = _memory_root(repo)
    note = memory / "notes" / "note.md"
    note.write_text("note", encoding="utf-8")
    hardlink = tmp_path / "note-hardlink.md"

    if os.name == "nt":
        original_read = diagnostics_module.workspace_files.read_regular_bytes_anchored
        read_calls = 0

        def add_hardlink_before_read(root, relative, **kwargs):
            nonlocal read_calls
            read_calls += 1
            os.link(note, hardlink)
            return original_read(root, relative, **kwargs)

        monkeypatch.setattr(
            diagnostics_module.workspace_files,
            "read_regular_bytes_anchored",
            add_hardlink_before_read,
        )
    else:
        original_stat = diagnostics_module.os.stat
        read_calls = 0

        def add_hardlink_before_final_stat(path, *args, **kwargs):
            nonlocal read_calls
            if path == "note.md" and kwargs.get("dir_fd") is not None:
                read_calls += 1
                if read_calls == 2:
                    os.link(note, hardlink)
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(
            diagnostics_module.os, "stat", add_hardlink_before_final_stat
        )

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user" / ".pony" / "memory",
    )

    assert read_calls == (1 if os.name == "nt" else 2)
    assert result["status"] == "unknown"
    assert {
        "path": "workspace/notes/note.md",
        "count": 1,
        "reason_code": "memory_file_unavailable",
        "limit": 0,
    } in result["issues"]


def test_doctor_keeps_memory_health_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    result = collect_doctor(tmp_path, args=None)

    assert result["memory"] == {
        "check_id": "memory",
        "status": "pass",
        "reason_code": "memory_diagnostics_passed",
        "remediation": "",
        "issues": [],
    }


def test_doctor_renders_the_compact_memory_health_check(tmp_path, monkeypatch, capsys):
    memory = _memory_root(tmp_path)
    canary = "memory-doctor-content-canary"
    (memory / "notes" / "note.md").write_text(
        f"---\ninvalid frontmatter\n---\n{canary}\n", encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    for output_format in ("json", "text"):
        assert main(["--cwd", str(tmp_path), "--format", output_format, "doctor"]) == 0
        output = capsys.readouterr().out
        assert canary not in output
        if output_format == "json":
            assert json.loads(output)["data"]["memory"]["status"] == "pass"
        else:
            assert "Memory" in output
