import json
import os
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
    original_read = diagnostics_module._read_bounded_at
    parent_identities = []

    def replace_root(parent_descriptor, name, expected, limit):
        if not parent_identities:
            memory.rename(displaced)
            replacement.rename(memory)
        parent_identities.append(
            diagnostics_module._identity(os.fstat(parent_descriptor))
        )
        return original_read(parent_descriptor, name, expected, limit)

    monkeypatch.setattr(diagnostics_module, "_read_bounded_at", replace_root)

    result = collect_memory_diagnostics(
        repo,
        user_memory_root=tmp_path / "missing-user" / ".pony" / "memory",
    )

    assert parent_identities == [old_notes_identity, old_root_identity]
    assert result["status"] == "unknown"
    assert {
        "path": "workspace",
        "count": 1,
        "reason_code": "memory_root_changed",
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
