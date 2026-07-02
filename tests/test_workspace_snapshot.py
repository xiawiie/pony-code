from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico import runtime as runtime_module
from pico.workspace_snapshot import capture_workspace_snapshot, diff_workspace_snapshots


def test_workspace_snapshot_scan_has_file_limit(tmp_path):
    for index in range(3):
        (tmp_path / f"file_{index}.txt").write_text(f"{index}\n", encoding="utf-8")

    snapshot = capture_workspace_snapshot(tmp_path, max_files=2)

    assert sorted(snapshot) == ["file_0.txt", "file_1.txt"]


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
