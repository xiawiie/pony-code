"""Task 11 · legacy topics -> notes/ 迁移集成测试.

覆盖 CLI `pico-cli memory migrate --apply` 的实际 apply 分支.
"""

from types import SimpleNamespace


def _args(cwd):
    return SimpleNamespace(format="text", cwd=str(cwd))


def test_migrate_apply_moves_topics_into_notes(tmp_path):
    from pico.cli_commands import handle_memory

    topics = tmp_path / ".pico" / "memory" / "topics"
    topics.mkdir(parents=True)
    (topics / "project-conventions.md").write_text("- use uv\n")
    (topics / "dependency-facts.md").write_text("- Python 3.10+\n")

    rc = handle_memory(["migrate", "--apply"], str(tmp_path), _args(tmp_path))
    assert rc == 0

    notes = tmp_path / ".pico" / "memory" / "notes"
    assert (notes / "project-conventions.md").exists()
    assert (notes / "dependency-facts.md").exists()
    assert (notes / "project-conventions.md").read_text(encoding="utf-8") == "- use uv\n"
    assert (topics / "project-conventions.md.deprecated").exists()
    assert (topics / "dependency-facts.md.deprecated").exists()
    assert not (topics / "project-conventions.md").exists()


def test_migrate_apply_preserves_existing_notes(tmp_path):
    """如果 notes/<same-name>.md 已存在，migrate 不能覆盖用户手写内容."""
    from pico.cli_commands import handle_memory

    topics = tmp_path / ".pico" / "memory" / "topics"
    notes = tmp_path / ".pico" / "memory" / "notes"
    topics.mkdir(parents=True)
    notes.mkdir(parents=True)
    (topics / "x.md").write_text("legacy\n")
    (notes / "x.md").write_text("user hand-written\n")

    rc = handle_memory(["migrate", "--apply"], str(tmp_path), _args(tmp_path))
    assert rc == 0

    assert (notes / "x.md").read_text(encoding="utf-8") == "user hand-written\n"


def test_migrate_preview_reports_but_no_writes(tmp_path):
    from pico.cli_commands import handle_memory

    topics = tmp_path / ".pico" / "memory" / "topics"
    topics.mkdir(parents=True)
    (topics / "y.md").write_text("- convention\n")

    rc = handle_memory(["migrate"], str(tmp_path), _args(tmp_path))
    assert rc == 0

    notes = tmp_path / ".pico" / "memory" / "notes"
    assert not notes.exists() or not (notes / "y.md").exists()
    assert (topics / "y.md").exists()
    assert not (topics / "y.md.deprecated").exists()
