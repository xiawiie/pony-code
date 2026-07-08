"""Task 22: pico-cli memory migrate (agent_notes.md → agent/legacy-import.md).

Contracts:
- Default apply: creates agent/legacy-import.md with legacy frontmatter,
  renames agent_notes.md → agent_notes.md.legacy, writes a backup copy.
- --dry-run: prints what would happen, does not touch the filesystem.
- --rollback: restores agent_notes.md from the .legacy rename and removes
  the created agent/legacy-import.md.
"""

from pico.cli_memory import cli_memory_migrate


def test_migrate_creates_legacy_import(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "agent_notes.md").write_text(
        "- 2026-01-01T00:00:00Z  first note\n", encoding="utf-8"
    )

    rc = cli_memory_migrate(workspace_root=ws, dry_run=False, rollback=False)
    assert rc == 0
    imported = ws / "agent" / "legacy-import.md"
    assert imported.exists()
    body = imported.read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "name: legacy-import" in body
    # Original file renamed to .legacy
    assert (ws / "agent_notes.md.legacy").exists()
    # Backup written into ws/backup/
    backups = list((ws / "backup").glob("agent_notes.md.*"))
    assert len(backups) == 1


def test_migrate_dry_run_no_write(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "agent_notes.md").write_text("orig", encoding="utf-8")
    rc = cli_memory_migrate(workspace_root=ws, dry_run=True, rollback=False)
    assert rc == 0
    assert not (ws / "agent" / "legacy-import.md").exists()
    assert not (ws / "agent_notes.md.legacy").exists()
    # agent_notes.md still there, unchanged
    assert (ws / "agent_notes.md").read_text(encoding="utf-8") == "orig"


def test_migrate_rollback(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "agent_notes.md").write_text("orig", encoding="utf-8")
    cli_memory_migrate(workspace_root=ws, dry_run=False, rollback=False)
    rc = cli_memory_migrate(workspace_root=ws, dry_run=False, rollback=True)
    assert rc == 0
    assert (ws / "agent_notes.md").exists()
    assert (ws / "agent_notes.md").read_text(encoding="utf-8") == "orig"
    assert not (ws / "agent" / "legacy-import.md").exists()


def test_migrate_no_source_file_returns_zero(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    rc = cli_memory_migrate(workspace_root=ws, dry_run=False, rollback=False)
    assert rc == 0
    assert not (ws / "agent" / "legacy-import.md").exists()


def test_rollback_without_legacy_file_returns_nonzero(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    rc = cli_memory_migrate(workspace_root=ws, dry_run=False, rollback=True)
    assert rc == 1  # nothing to roll back
