from pico import Pico, SessionStore, WorkspaceContext
from pico import runtime as runtime_module
from pico import workspace_snapshot as snapshot_module
from pico.providers.fake import FakeModelClient
from pico.workspace_snapshot import capture_workspace_snapshot, diff_workspace_snapshots


def test_workspace_snapshot_scan_has_file_limit(tmp_path):
    for index in range(3):
        (tmp_path / f"file_{index}.txt").write_text(f"{index}\n", encoding="utf-8")

    snapshot = capture_workspace_snapshot(tmp_path, max_files=2)

    assert sorted(snapshot) == ["file_0.txt", "file_1.txt"]


def test_workspace_snapshot_skips_sensitive_paths_before_stat_or_hash(
    tmp_path,
    monkeypatch,
):
    sensitive = tmp_path / ".env"
    sensitive.write_text("PICO_TOKEN=opaque\n", encoding="utf-8")
    safe = tmp_path / "safe.txt"
    safe.write_text("safe\n", encoding="utf-8")
    real_stat = type(safe).stat
    calls = []

    def guarded_stat(self, *args, **kwargs):
        if self == sensitive:
            raise AssertionError("sensitive path stat")
        return real_stat(self, *args, **kwargs)

    def fake_hash(path):
        assert path != sensitive
        calls.append(path)
        return {
            "content_hash": "a" * 64,
            "size_bytes": path.stat().st_size,
        }

    monkeypatch.setattr(type(safe), "stat", guarded_stat)
    monkeypatch.setattr(snapshot_module, "hash_file_bytes", fake_hash, raising=False)

    snapshot = capture_workspace_snapshot(tmp_path)

    assert ".env" not in snapshot
    assert snapshot["safe.txt"] == "a" * 64
    assert calls == [safe]


def test_workspace_snapshot_skips_leaf_symlinks(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("safe\n", encoding="utf-8")
    (tmp_path / "alias.txt").symlink_to(target)

    snapshot = capture_workspace_snapshot(tmp_path)

    assert "target.txt" in snapshot
    assert "alias.txt" not in snapshot


def test_workspace_snapshot_skips_allowed_template_name_used_as_directory(
    tmp_path,
):
    nested = tmp_path / ".env.example" / "child.txt"
    nested.parent.mkdir()
    nested.write_text("must not hash\n", encoding="utf-8")

    snapshot = capture_workspace_snapshot(tmp_path)

    assert ".env.example/child.txt" not in snapshot


def test_workspace_snapshot_diff_reports_ordered_changes():
    changed, summaries = diff_workspace_snapshots(
        {"a.txt": "old", "b.txt": "same", "deleted.txt": "gone"},
        {"a.txt": "new", "b.txt": "same", "created.txt": "new"},
    )

    assert changed == ["a.txt", "created.txt", "deleted.txt"]
    assert summaries == ["modified:a.txt", "created:created.txt", "deleted:deleted.txt"]


def test_pico_workspace_snapshot_methods_delegate(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
    )
    calls = []

    def fake_capture(root):
        calls.append(root)
        return {"README.md": "digest"}

    monkeypatch.setattr(runtime_module.workspace_snapshot, "capture_workspace_snapshot", fake_capture)

    assert agent.capture_workspace_snapshot() == {"README.md": "digest"}
    assert calls == [tmp_path.resolve()]
