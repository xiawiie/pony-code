from pathlib import Path

from pico.memory.block_store import BlockStore
from pico.memory.refresher import MemoryRefresher
from pico.repo_map import RepoMap


def _setup(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    user.mkdir()
    (workspace / "notes" / "auth.md").write_text("# Auth\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("class Foo: pass\n")
    store = BlockStore(workspace_root=workspace, user_root=user)
    repo_map = RepoMap(repo_root=tmp_path)
    repo_map.scan()
    return MemoryRefresher(store, repo_map)


def test_first_call_produces_snapshot(tmp_path):
    r = _setup(tmp_path)
    snap = r.refresh_if_stale()
    assert "workspace/notes/auth.md" in snap.memory_index_text
    assert "src" in snap.project_structure_text


def test_snapshot_stable_when_nothing_changes(tmp_path):
    r = _setup(tmp_path)
    a = r.refresh_if_stale()
    b = r.refresh_if_stale()
    assert a.memory_index_text == b.memory_index_text
    assert a.project_structure_text == b.project_structure_text


def test_snapshot_updates_when_new_note(tmp_path):
    r = _setup(tmp_path)
    a = r.refresh_if_stale()
    (Path(r.store.workspace_root) / "notes" / "testing.md").write_text("# Tests\n")
    b = r.refresh_if_stale()
    assert "testing.md" not in a.memory_index_text
    assert "testing.md" in b.memory_index_text


def test_snapshot_updates_when_new_dir(tmp_path):
    r = _setup(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("class T: pass\n")
    snap = r.refresh_if_stale()
    assert "tests" in snap.project_structure_text
