import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

import pico.recovery_manager as recovery_manager_module
import pico.sandbox_session as session_module
from pico.checkpoint_store import CheckpointStore
from pico.sandbox_session import (
    find_project_sandbox_session,
    SandboxSessionError,
    SandboxSessionStore,
    source_mutation_authority,
    WorkspaceView,
    snapshot_source_tree,
    stage_source,
)
from pico.security import ensure_private_dir


def _bootstrap(request):
    view = request.workspace_view
    tracked_paths = request.tracked_paths
    assert isinstance(view, WorkspaceView)
    assert isinstance(tracked_paths, tuple)
    git = view.physical_root / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/pico-sandbox\n", encoding="utf-8")
    return "a" * 40


def _session_metadata():
    return {
        "engine": {
            "endpoint_hash": "sha256:" + "1" * 64,
            "client_version": "29.5.2",
            "server_version": "29.5.2",
            "api_version": "1.54",
            "profile": "desktop_vm",
            "security_digest": "sha256:" + "2" * 64,
        },
        "image": {
            "reference": "example.invalid/pico@sha256:" + "3" * 64,
            "manifest_digest": "sha256:" + "3" * 64,
            "image_id": "sha256:" + "4" * 64,
            "platform": "linux/arm64",
        },
        "policy": {
            "version": 1,
            "digest": "sha256:" + "5" * 64,
            "network": "none",
            "mount_digest": "sha256:" + "6" * 64,
            "resource_digest": "sha256:" + "7" * 64,
        },
    }


def _create(tmp_path, **kwargs):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("demo\n", encoding="utf-8")
    store = SandboxSessionStore(tmp_path / "sandboxes")
    options = _session_metadata()
    options.update(kwargs)
    session = store.create(
        source,
        pico_session_id="session-1",
        bootstrap_git=_bootstrap,
        **options,
    )
    return source, store, session


def _tree_snapshot(root):
    result = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        result.append(
            (
                path.relative_to(root).as_posix(),
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
        )
    return result


def test_workspace_view_maps_only_relative_or_workspace_paths(tmp_path):
    root = tmp_path / "workspace"
    nested = root / "src"
    nested.mkdir(parents=True)
    target = nested / "main.py"
    target.write_text("pass\n", encoding="utf-8")
    view = WorkspaceView(root)

    assert view.physical_path("src/main.py") == target
    assert view.physical_path("/workspace/src/main.py") == target
    assert view.logical_path(target) == "/workspace/src/main.py"

    for unsafe in ("../outside", "/etc/passwd", "/workspace/../outside"):
        with pytest.raises(SandboxSessionError, match="workspace_path_invalid"):
            view.physical_path(unsafe)
    link = root / "link"
    link.symlink_to(target)
    with pytest.raises(SandboxSessionError, match="workspace_path_invalid"):
        view.physical_path("link")


def test_stage_source_filters_state_secrets_and_generated_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "secret.txt").write_text("known-secret\n", encoding="utf-8")
    (source / ".env").write_text("TOKEN=value\n", encoding="utf-8")
    (source / ".env.example").write_text("URL=https://example.invalid\n", encoding="utf-8")
    (source / ".pico").mkdir()
    (source / ".pico" / "run.json").write_text("{}", encoding="utf-8")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "package.js").write_text("x", encoding="utf-8")

    result = stage_source(
        source,
        tmp_path / "staging",
        known_secrets=(b"known-secret",),
    )

    assert {entry["path"] for entry in result["entries"]} == {
        ".env.example",
        "main.py",
    }
    assert result["excluded_counts"] == {
        "excluded_generated": 1,
        "excluded_pico_state": 1,
        "known_secret_content": 1,
        "sensitive_path": 1,
    }


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo"))
def test_stage_source_rejects_unsupported_entries(tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    ordinary = source / "ordinary"
    ordinary.write_text("data", encoding="utf-8")
    candidate = source / "candidate"
    if kind == "symlink":
        candidate.symlink_to("ordinary")
    elif kind == "hardlink":
        os.link(ordinary, candidate)
    else:
        os.mkfifo(candidate)

    with pytest.raises(SandboxSessionError, match="unsupported_workspace_entry"):
        stage_source(source, tmp_path / "staging")


def test_stage_source_rejects_casefold_collision(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "A.txt").write_text("data", encoding="utf-8")
    monkeypatch.setattr(
        session_module,
        "_walk_inventory",
        lambda _source, **_kwargs: ({"A.txt", "a.txt"}, set(), {}),
    )

    with pytest.raises(SandboxSessionError, match="workspace_path_collision"):
        stage_source(source, tmp_path / "staging")


def test_stage_source_rejects_file_over_capacity(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "large.bin").write_bytes(b"1234")
    monkeypatch.setattr(session_module, "MAX_FILE_BYTES", 3)

    with pytest.raises(SandboxSessionError, match="workspace_capacity_exceeded"):
        stage_source(source, tmp_path / "staging")


def test_stage_source_rejects_source_change_between_passes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "main.py"
    path.write_text("before\n", encoding="utf-8")
    original = session_module._read_source_file
    calls = 0

    def mutate_after_first(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            path.write_text("after\n", encoding="utf-8")
        return result

    monkeypatch.setattr(session_module, "_read_source_file", mutate_after_first)

    with pytest.raises(SandboxSessionError, match="workspace_changed_during_stage"):
        stage_source(source, tmp_path / "staging")


def test_stage_source_rejects_new_file_between_inventory_passes(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("before\n", encoding="utf-8")
    original = session_module._publish_file

    def add_file_after_copy(*args, **kwargs):
        result = original(*args, **kwargs)
        (source / "added.py").write_text("late\n", encoding="utf-8")
        return result

    monkeypatch.setattr(session_module, "_publish_file", add_file_after_copy)

    with pytest.raises(SandboxSessionError, match="workspace_changed_during_stage"):
        stage_source(source, tmp_path / "staging")


def test_stage_source_rejects_nested_mount_before_copy(tmp_path, monkeypatch):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "file").write_text("data", encoding="utf-8")
    monkeypatch.setattr(
        session_module.os.path,
        "ismount",
        lambda path: Path(path).name == "nested",
    )

    with pytest.raises(SandboxSessionError, match="workspace_mount_boundary"):
        stage_source(source, tmp_path / "staging")


def test_snapshot_source_tree_is_stable_unfiltered_and_metadata_bound(tmp_path):
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    (source / ".pico").mkdir()
    path = source / "main.py"
    path.write_text("before\n", encoding="utf-8")
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (source / ".pico" / "state").write_text("state\n", encoding="utf-8")

    initial = snapshot_source_tree(source)

    assert snapshot_source_tree(source) == initial
    path.write_text("after\n", encoding="utf-8")
    content_changed = snapshot_source_tree(source)
    assert content_changed != initial
    path.chmod(0o700)
    mode_changed = snapshot_source_tree(source)
    assert mode_changed != content_changed
    (source / ".git" / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    git_changed = snapshot_source_tree(source)
    assert git_changed != mode_changed
    (source / ".pico" / "state").write_text("changed\n", encoding="utf-8")
    assert snapshot_source_tree(source) != git_changed


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo"))
def test_snapshot_source_tree_rejects_nonordinary_entries(tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    ordinary = source / "ordinary"
    ordinary.write_text("data", encoding="utf-8")
    candidate = source / "candidate"
    if kind == "symlink":
        candidate.symlink_to("ordinary")
    elif kind == "hardlink":
        os.link(ordinary, candidate)
    else:
        os.mkfifo(candidate)

    with pytest.raises(SandboxSessionError, match="unsupported_workspace_entry"):
        snapshot_source_tree(source)


def test_snapshot_source_tree_rejects_mount_capacity_and_race(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    path = nested / "file"
    path.write_text("before\n", encoding="utf-8")
    monkeypatch.setattr(
        session_module.os.path,
        "ismount",
        lambda value: Path(value).name == "nested",
    )
    with pytest.raises(SandboxSessionError, match="workspace_mount_boundary"):
        snapshot_source_tree(source)

    monkeypatch.setattr(session_module.os.path, "ismount", lambda _value: False)
    monkeypatch.setattr(session_module, "MAX_ENTRIES", 1)
    with pytest.raises(SandboxSessionError, match="workspace_capacity_exceeded"):
        snapshot_source_tree(source)

    monkeypatch.setattr(session_module, "MAX_ENTRIES", 100_000)
    original = session_module._read_source_file
    calls = 0

    def mutate_between_captures(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            path.write_text("after\n", encoding="utf-8")
        return result

    monkeypatch.setattr(session_module, "_read_source_file", mutate_between_captures)
    with pytest.raises(SandboxSessionError, match="workspace_changed_during_stage"):
        snapshot_source_tree(source)


def test_snapshot_source_tree_rejects_source_root_replacement(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "file"
    path.write_text("before\n", encoding="utf-8")
    original = session_module._read_source_file

    def replace_root(*args, **kwargs):
        result = original(*args, **kwargs)
        source.rename(tmp_path / "detached")
        source.mkdir()
        (source / "file").write_text("replacement\n", encoding="utf-8")
        return result

    monkeypatch.setattr(session_module, "_read_source_file", replace_root)

    with pytest.raises(SandboxSessionError, match="workspace_changed_during_stage"):
        snapshot_source_tree(source)


def test_git_staging_preserves_tracked_classification_and_audit(tmp_path):
    git = shutil.which("git")
    assert git is not None
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run([git, "init", "-q", source], check=True)
    subprocess.run(
        [git, "-C", source, "config", "user.name", "Pico Test"],
        check=True,
    )
    subprocess.run(
        [git, "-C", source, "config", "user.email", "pico@example.invalid"],
        check=True,
    )
    (source / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run([git, "-C", source, "add", "tracked.txt"], check=True)
    subprocess.run([git, "-C", source, "commit", "-qm", "baseline"], check=True)
    (source / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    branch = subprocess.run(
        [git, "-C", source, "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    head = subprocess.run(
        [git, "-C", source, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    store = SandboxSessionStore(tmp_path / "sandboxes")

    session = store.create(
        source,
        pico_session_id="session-1",
        bootstrap_git=_bootstrap,
        git_executable=git,
        **_session_metadata(),
    )

    baseline = json.loads((session.state_root / "baseline.json").read_text())
    assert baseline["tracked_paths"] == ["tracked.txt"]
    assert baseline["untracked_paths"] == ["untracked.txt"]
    assert session.manifest["source"]["branch"] == branch
    assert session.manifest["source"]["head"] == head


def test_create_persists_exact_manifest_baseline_and_sidecar(tmp_path):
    project_state = tmp_path / "project-state"
    source, store, session = _create(
        tmp_path,
        project_state_root=project_state,
    )

    assert session.state == "ready"
    assert session.workspace_view.physical_path("README.md").read_text() == "demo\n"
    assert session.manifest["source"]["root"] == str(source)
    assert session.manifest["execution"]["synthetic_git_commit"] == "a" * 40
    assert session.manifest["engine"] == _session_metadata()["engine"]
    assert session.manifest["image"] == _session_metadata()["image"]
    assert session.manifest["policy"] == _session_metadata()["policy"]
    assert session.manifest["lease"]["owner_pid"] == os.getpid()
    assert (session.state_root / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert session.workspace_view.physical_root.stat().st_mode & 0o777 == 0o755
    baseline = json.loads((session.state_root / "baseline.json").read_text())
    assert baseline["tree_digest"] == session.manifest["execution"]["tree_digest"]
    sidecar = project_state / "sandbox_sessions" / f"{session.sandbox_id}.json"
    source_info = source.lstat()
    state_info = session.state_root.lstat()
    assert json.loads(sidecar.read_text()) == {
        "format_version": 1,
        "pico_session_id": "session-1",
        "record_type": "docker_sandbox_session_pointer",
        "sandbox_id": session.sandbox_id,
        "source_device": source_info.st_dev,
        "source_inode": source_info.st_ino,
        "source_root": str(source),
        "state_device": state_info.st_dev,
        "state_inode": state_info.st_ino,
        "state_root": str(session.state_root),
    }
    assert store.inspect(session.state_root).manifest == session.manifest


def test_sidecar_is_immutable_across_manifest_state_updates(tmp_path):
    project_state = tmp_path / "project-state"
    _, store, session = _create(tmp_path, project_state_root=project_state)
    sidecar = Path(session.manifest["sidecar"]["path"])
    before = sidecar.read_bytes(), (sidecar.stat().st_dev, sidecar.stat().st_ino)

    nonce = session.manifest["lease"]["owner_nonce"]
    store.release(session.state_root, nonce)
    store.acquire(session.state_root)
    store.begin_call(
        session.state_root,
        call_id="call-1",
        reconciliation_token="c" * 64,
        container_name="pico-call-1",
        expected_labels={"io.pico.call": "call-1"},
        plan_digest="sha256:" + "d" * 64,
    )
    store.finish_call(session.state_root)

    assert (sidecar.read_bytes(), (sidecar.stat().st_dev, sidecar.stat().st_ino)) == before


def test_manifest_update_does_not_attempt_old_second_sidecar_write(
    tmp_path,
    monkeypatch,
):
    project_state = tmp_path / "project-state"
    _, store, session = _create(tmp_path, project_state_root=project_state)
    original = session_module._atomic_json

    def fail_old_second_write(path, *args, **kwargs):
        if Path(path).parent.name == "sandbox_sessions":
            raise OSError("old second write failed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(session_module, "_atomic_json", fail_old_second_write)
    nonce = session.manifest["lease"]["owner_nonce"]

    released = store.release(session.state_root, nonce)

    assert released.manifest["lease"] is None
    assert store.inspect(session.state_root).manifest == released.manifest


def test_manifest_update_rejects_sidecar_tamper_before_manifest_write(tmp_path):
    project_state = tmp_path / "project-state"
    _, store, session = _create(tmp_path, project_state_root=project_state)
    sidecar = Path(session.manifest["sidecar"]["path"])
    value = json.loads(sidecar.read_text())
    value["source_inode"] += 1
    sidecar.write_text(json.dumps(value), encoding="utf-8")
    sidecar.chmod(0o600)
    manifest = session.state_root / "manifest.json"
    manifest_before = manifest.read_bytes()

    with pytest.raises(SandboxSessionError, match="sandbox_manifest_invalid"):
        store.release(
            session.state_root,
            session.manifest["lease"]["owner_nonce"],
        )

    assert manifest.read_bytes() == manifest_before


def test_create_reconciles_sidecar_published_before_initial_manifest(
    tmp_path,
    monkeypatch,
):
    project_state = tmp_path / "project-state"
    original = session_module._atomic_json

    def crash_before_manifest(path, *args, **kwargs):
        if Path(path).name == "manifest.json":
            raise OSError("crash before initial manifest")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(session_module, "_atomic_json", crash_before_manifest)
    with pytest.raises(OSError, match="crash before initial manifest"):
        _create(tmp_path, project_state_root=project_state)

    sidecar = next((project_state / "sandbox_sessions").glob("sandbox_*.json"))
    pointer = json.loads(sidecar.read_text())
    orphan_root = Path(pointer["state_root"])
    assert list(orphan_root.iterdir()) == []

    monkeypatch.setattr(session_module, "_atomic_json", original)
    source = tmp_path / "source"
    store = SandboxSessionStore(tmp_path / "sandboxes")
    session = store.create(
        source,
        pico_session_id="session-2",
        bootstrap_git=_bootstrap,
        project_state_root=project_state,
        **_session_metadata(),
    )

    assert not orphan_root.exists()
    assert not sidecar.exists()
    assert store.inspect(session.state_root).state == "ready"


def test_create_resumes_creation_orphan_cleanup_after_root_removal(
    tmp_path,
    monkeypatch,
):
    project_state = tmp_path / "project-state"
    original = session_module._atomic_json

    def crash_before_manifest(path, *args, **kwargs):
        if Path(path).name == "manifest.json":
            raise OSError("crash before initial manifest")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(session_module, "_atomic_json", crash_before_manifest)
    with pytest.raises(OSError, match="crash before initial manifest"):
        _create(tmp_path, project_state_root=project_state)
    sidecar = next((project_state / "sandbox_sessions").glob("sandbox_*.json"))
    orphan_root = Path(json.loads(sidecar.read_text())["state_root"])
    orphan_root.rmdir()

    monkeypatch.setattr(session_module, "_atomic_json", original)
    source = tmp_path / "source"
    store = SandboxSessionStore(tmp_path / "sandboxes")
    store.create(
        source,
        pico_session_id="session-2",
        bootstrap_git=_bootstrap,
        project_state_root=project_state,
        **_session_metadata(),
    )

    assert not sidecar.exists()


def test_initial_sidecar_publish_collision_never_writes_manifest(
    tmp_path,
    monkeypatch,
):
    project_state = tmp_path / "project-state"

    def collide(*_args, **_kwargs):
        raise FileExistsError("sidecar collision")

    monkeypatch.setattr(recovery_manager_module, "_rename_noreplace", collide)

    with pytest.raises(SandboxSessionError, match="sandbox_state_invalid"):
        _create(tmp_path, project_state_root=project_state)

    state_root = next((tmp_path / "sandboxes").glob("*/sandbox_*"))
    assert list(state_root.iterdir()) == []
    assert list((project_state / "sandbox_sessions").iterdir()) == []


def test_create_reconciles_sidecar_after_publish_fsync_failure(
    tmp_path,
    monkeypatch,
):
    project_state = tmp_path / "project-state"
    original_rename = recovery_manager_module._rename_noreplace
    original_fsync = session_module.os.fsync
    published = False

    def publish(*args, **kwargs):
        nonlocal published
        result = original_rename(*args, **kwargs)
        published = True
        return result

    def fail_after_publish(descriptor):
        if published:
            raise OSError("sidecar parent fsync failed")
        return original_fsync(descriptor)

    monkeypatch.setattr(recovery_manager_module, "_rename_noreplace", publish)
    monkeypatch.setattr(session_module.os, "fsync", fail_after_publish)

    with pytest.raises(SandboxSessionError, match="sandbox_state_invalid"):
        _create(tmp_path, project_state_root=project_state)

    sidecar = next((project_state / "sandbox_sessions").glob("sandbox_*.json"))
    orphan_root = Path(json.loads(sidecar.read_text())["state_root"])
    assert list(orphan_root.iterdir()) == []

    monkeypatch.setattr(session_module.os, "fsync", original_fsync)
    monkeypatch.setattr(
        recovery_manager_module,
        "_rename_noreplace",
        original_rename,
    )
    source = tmp_path / "source"
    store = SandboxSessionStore(tmp_path / "sandboxes")
    store.create(
        source,
        pico_session_id="session-2",
        bootstrap_git=_bootstrap,
        project_state_root=project_state,
        **_session_metadata(),
    )

    assert not orphan_root.exists()
    assert not sidecar.exists()


def test_create_rejects_bootstrap_modifying_ordinary_workspace(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("before\n", encoding="utf-8")
    store = SandboxSessionStore(tmp_path / "sandboxes")

    def tamper(request):
        view = request.workspace_view
        (view.physical_root / "README.md").write_text("after\n", encoding="utf-8")
        return "a" * 40

    with pytest.raises(
        SandboxSessionError,
        match="synthetic_git_modified_workspace",
    ):
        store.create(
            source,
            pico_session_id="session-1",
            bootstrap_git=tamper,
            **_session_metadata(),
        )

    state_root = next((tmp_path / "sandboxes").glob("*/sandbox_*"))
    manifest = json.loads((state_root / "manifest.json").read_text())
    assert manifest["state"] == "failed"
    assert manifest["cleanup"]["status"] == "complete"
    assert not (state_root / "workspace").exists()
    assert not (state_root / "workspace.candidate").exists()


def test_create_verifies_normalized_staging_mode_after_bootstrap(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "README.md"
    path.write_text("demo\n", encoding="utf-8")
    path.chmod(0o664)
    store = SandboxSessionStore(tmp_path / "sandboxes")

    session = store.create(
        source,
        pico_session_id="session-1",
        bootstrap_git=_bootstrap,
        **_session_metadata(),
    )

    staged = session.workspace_view.physical_path("README.md")
    assert staged.stat().st_mode & 0o777 == 0o644


def test_list_is_zero_mutation_when_parent_is_absent(tmp_path):
    parent = tmp_path / "missing"
    store = SandboxSessionStore(parent)

    assert store.list() == []
    assert not parent.exists()


def test_inventory_ignores_exact_inactive_source_control_workspace(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    parent = tmp_path / "sandboxes"

    with source_mutation_authority(parent, source):
        pass

    store = SandboxSessionStore(parent)
    assert store.inventory() == {"manifests": [], "unknown_count": 0}
    assert store.list() == []


@pytest.mark.parametrize("tamper", ("lock_bytes", "active", "sibling"))
def test_inventory_rejects_tampered_source_control_only_workspace(
    tmp_path,
    tamper,
):
    source = tmp_path / "source"
    source.mkdir()
    parent = tmp_path / "sandboxes"
    with source_mutation_authority(parent, source):
        pass
    control = next(parent.glob("*/.source-apply-control"))
    if tamper == "lock_bytes":
        (control / ".lock").write_bytes(b"tampered")
    elif tamper == "active":
        (control / "active.json").write_bytes(b"{invalid")
        (control / "active.json").chmod(0o600)
    else:
        (control / "unexpected").write_bytes(b"")
        (control / "unexpected").chmod(0o600)

    store = SandboxSessionStore(parent)
    assert store.inventory()["unknown_count"] == 1
    with pytest.raises(SandboxSessionError, match="sandbox_state_invalid"):
        store.list()


def test_create_rejects_missing_identity_metadata_before_state_mutation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("demo\n", encoding="utf-8")
    parent = tmp_path / "sandboxes"
    store = SandboxSessionStore(parent)

    with pytest.raises(SandboxSessionError, match="sandbox_manifest_invalid"):
        store.create(
            source,
            pico_session_id="session-1",
            bootstrap_git=_bootstrap,
        )

    assert not parent.exists()


def test_inspect_rejects_duplicate_keys_and_symlink_manifest(tmp_path):
    _, store, session = _create(tmp_path)
    manifest_path = session.state_root / "manifest.json"
    manifest_path.write_text('{"record_type":1,"record_type":2}', encoding="utf-8")
    manifest_path.chmod(0o600)

    with pytest.raises(SandboxSessionError, match="sandbox_manifest_invalid"):
        store.inspect(session.state_root)

    manifest_path.unlink()
    manifest_path.symlink_to(session.state_root / "baseline.json")
    with pytest.raises(SandboxSessionError, match="sandbox_manifest_invalid"):
        store.inspect(session.state_root)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    (
        ("diff", "unexpected", 1),
        ("diff", "candidate_count", "0"),
        ("execution", "file_count", True),
        ("engine", "unexpected", 1),
        ("engine", "profile", []),
        ("engine", "api_version", 1.54),
        ("image", "manifest_digest", "invalid"),
        ("image", "platform", []),
        ("policy", "network", "bridge"),
    ),
)
def test_inspect_rejects_unknown_or_invalid_nested_manifest_values(
    tmp_path,
    section,
    key,
    value,
):
    _, store, session = _create(tmp_path)
    manifest_path = session.state_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[section][key] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)

    with pytest.raises(SandboxSessionError, match="sandbox_manifest_invalid"):
        store.inspect(session.state_root)


def test_inspect_rejects_baseline_and_sidecar_identity_tampering(tmp_path):
    project_state = tmp_path / "project-state"
    _, store, session = _create(tmp_path, project_state_root=project_state)
    baseline_path = session.state_root / "baseline.json"
    baseline = json.loads(baseline_path.read_text())
    baseline["unexpected"] = True
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    baseline_path.chmod(0o600)

    with pytest.raises(SandboxSessionError, match="sandbox_manifest_invalid"):
        store.inspect(session.state_root)

    session = store.create(
        tmp_path / "source",
        pico_session_id="session-2",
        bootstrap_git=_bootstrap,
        project_state_root=project_state,
        **_session_metadata(),
    )
    sidecar_path = Path(session.manifest["sidecar"]["path"])
    sidecar = json.loads(sidecar_path.read_text())
    sidecar["source_inode"] += 1
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    sidecar_path.chmod(0o600)

    with pytest.raises(SandboxSessionError, match="sandbox_manifest_invalid"):
        store.inspect(session.state_root)


def test_find_project_sandbox_session_is_zero_write_and_exact(tmp_path):
    project_state = tmp_path / "project-state"
    source, _store, session = _create(
        tmp_path,
        project_state_root=project_state,
    )
    missing = tmp_path / "missing-project-state"
    before = _tree_snapshot(tmp_path)

    assert find_project_sandbox_session(missing, source, "session-1") is None
    assert _tree_snapshot(tmp_path) == before
    bound_before = _tree_snapshot(tmp_path)
    assert find_project_sandbox_session(project_state, source, "other-session") is None
    found = find_project_sandbox_session(project_state, source, "session-1")

    assert found is not None
    assert found.state_root == session.state_root
    assert found.manifest == session.manifest
    assert _tree_snapshot(tmp_path) == bound_before


def test_find_project_sandbox_session_rejects_duplicate_binding(tmp_path):
    project_state = tmp_path / "project-state"
    source, store, _session = _create(
        tmp_path,
        project_state_root=project_state,
    )
    store.create(
        source,
        pico_session_id="session-1",
        bootstrap_git=_bootstrap,
        project_state_root=project_state,
        **_session_metadata(),
    )

    with pytest.raises(SandboxSessionError, match="sandbox_state_invalid"):
        find_project_sandbox_session(project_state, source, "session-1")


@pytest.mark.parametrize("tamper", ("unknown", "identity"))
def test_find_project_sandbox_session_rejects_unknown_or_tampered_sidecar(
    tmp_path,
    tamper,
):
    project_state = tmp_path / "project-state"
    source, _store, session = _create(
        tmp_path,
        project_state_root=project_state,
    )
    sidecar = Path(session.manifest["sidecar"]["path"])
    if tamper == "unknown":
        unknown = sidecar.parent / "unknown"
        unknown.write_text("unknown", encoding="utf-8")
        unknown.chmod(0o600)
    else:
        value = json.loads(sidecar.read_text())
        value["state_inode"] += 1
        sidecar.write_text(json.dumps(value), encoding="utf-8")
        sidecar.chmod(0o600)

    with pytest.raises(SandboxSessionError, match="sandbox_state_invalid"):
        find_project_sandbox_session(project_state, source, "session-1")


def test_list_and_inspect_are_zero_mutation(tmp_path):
    _, store, session = _create(tmp_path)
    before = _tree_snapshot(tmp_path)

    assert store.inspect(session.state_root).sandbox_id == session.sandbox_id
    assert [item["sandbox_id"] for item in store.list()] == [session.sandbox_id]

    assert _tree_snapshot(tmp_path) == before

def test_lease_acquire_release_and_busy_detection(tmp_path, monkeypatch):
    _, store, session = _create(tmp_path)
    acquired = store.acquire(session.state_root)
    nonce = acquired.manifest["lease"]["owner_nonce"]

    released = store.release(session.state_root, nonce)
    assert released.manifest["lease"] is None

    reacquired = store.acquire(session.state_root)
    manifest = reacquired.manifest
    manifest["lease"]["owner_pid"] = os.getpid() + 100000
    session_module._atomic_json(
        session.state_root / "manifest.json",
        session.state_root,
        manifest,
    )
    monkeypatch.setattr(session_module, "_lease_is_live", lambda _lease: True)
    with pytest.raises(SandboxSessionError, match="sandbox_session_busy"):
        store.acquire(session.state_root)


def test_live_lease_identity_probe_failure_is_not_treated_as_dead(monkeypatch):
    monkeypatch.setattr(session_module.os, "kill", lambda _pid, _signal: None)

    def unavailable(_pid):
        raise SandboxSessionError("sandbox_lease_identity_unavailable")

    monkeypatch.setattr(session_module, "_process_start", unavailable)

    with pytest.raises(
        SandboxSessionError,
        match="sandbox_lease_identity_unavailable",
    ):
        session_module._lease_is_live(
            {
                "owner_pid": os.getpid(),
                "owner_start": "unknown",
            }
        )


def test_mutating_methods_require_current_live_lease(tmp_path):
    _, store, session = _create(tmp_path)
    manifest = session.manifest
    manifest["lease"] = None
    session_module._atomic_json(
        session.state_root / "manifest.json",
        session.state_root,
        manifest,
    )

    with pytest.raises(SandboxSessionError, match="sandbox_lease_mismatch"):
        store.begin_call(
            session.state_root,
            call_id="call-1",
            reconciliation_token="c" * 64,
            container_name="pico-call-1",
            expected_labels={"io.pico.call": "call-1"},
            plan_digest="sha256:" + "d" * 64,
        )


def test_active_call_is_persisted_before_id_and_reconciled(tmp_path):
    _, store, session = _create(tmp_path)
    labels = {"io.pico.call": "call-1", "io.pico.token": "c" * 64}
    running = store.begin_call(
        session.state_root,
        call_id="call-1",
        reconciliation_token="c" * 64,
        container_name="pico-call-1",
        expected_labels=labels,
        plan_digest="sha256:" + "d" * 64,
    )
    assert running.state == "running"
    assert running.manifest["active_call"]["container_id"] == ""

    reconciled = store.reconcile_active_call(
        session.state_root,
        lambda _active: [
            {
                "id": "e" * 64,
                "name": "pico-call-1",
                "labels": labels,
                "contract_matches": True,
            }
        ],
    )
    assert reconciled.manifest["active_call"]["container_id"] == "e" * 64
    assert store.finish_call(session.state_root).state == "ready"


def test_reconciliation_refuses_multiple_or_mismatched_matches(tmp_path):
    _, store, session = _create(tmp_path)
    labels = {"io.pico.call": "call-1"}
    store.begin_call(
        session.state_root,
        call_id="call-1",
        reconciliation_token="c" * 64,
        container_name="pico-call-1",
        expected_labels=labels,
        plan_digest="sha256:" + "d" * 64,
    )
    ambiguous = store.reconcile_active_call(
        session.state_root,
        lambda _active: [{}, {}],
    )
    assert ambiguous.state == "review_required"


def test_reconciliation_requires_full_inspect_match(tmp_path):
    _, store, session = _create(tmp_path)
    labels = {"io.pico.call": "call-1"}
    store.begin_call(
        session.state_root,
        call_id="call-1",
        reconciliation_token="c" * 64,
        container_name="pico-call-1",
        expected_labels=labels,
        plan_digest="sha256:" + "d" * 64,
    )

    reconciled = store.reconcile_active_call(
        session.state_root,
        lambda _active: [
            {
                "id": "e" * 64,
                "name": "pico-call-1",
                "labels": labels,
                "contract_matches": False,
            }
        ],
    )

    assert reconciled.state == "review_required"


def test_reconciliation_with_recorded_id_refuses_missing_container(tmp_path):
    _, store, session = _create(tmp_path)
    store.begin_call(
        session.state_root,
        call_id="call-1",
        reconciliation_token="c" * 64,
        container_name="pico-call-1",
        expected_labels={"io.pico.call": "call-1"},
        plan_digest="sha256:" + "d" * 64,
    )
    store.record_container_id(session.state_root, "e" * 64)

    reconciled = store.reconcile_active_call(
        session.state_root,
        lambda _active: [],
    )

    assert reconciled.state == "review_required"


@pytest.mark.parametrize("return_state", ("ready", "creating"))
def test_reconciliation_with_recorded_id_accepts_confirmed_exact_absence(
    tmp_path,
    return_state,
):
    _, store, session = _create(tmp_path)
    if return_state == "creating":
        manifest = store.inspect(session.state_root).manifest
        manifest["state"] = "creating"
        session_module._atomic_json(
            session.state_root / "manifest.json",
            session.state_root,
            manifest,
        )
    store.begin_call(
        session.state_root,
        call_id="call-1",
        reconciliation_token="c" * 64,
        container_name="pico-call-1",
        expected_labels={"io.pico.call": "call-1"},
        plan_digest="sha256:" + "d" * 64,
        return_state=return_state,
    )
    container_id = "e" * 64
    store.record_container_id(session.state_root, container_id)
    checked = []

    reconciled = store.reconcile_active_call(
        session.state_root,
        lambda _active: [],
        confirm_container_absent=lambda exact_id: checked.append(exact_id) or True,
    )

    assert checked == [container_id]
    assert reconciled.state == return_state
    assert reconciled.manifest["active_call"] is None


def test_finish_call_preserves_identity_when_review_is_required(tmp_path):
    _, store, session = _create(tmp_path)
    store.begin_call(
        session.state_root,
        call_id="call-1",
        reconciliation_token="c" * 64,
        container_name="pico-call-1",
        expected_labels={"io.pico.call": "call-1"},
        plan_digest="sha256:" + "d" * 64,
    )
    store.record_container_id(session.state_root, "e" * 64)

    reviewed = store.finish_call(session.state_root, review_required=True)

    assert reviewed.state == "review_required"
    assert reviewed.manifest["active_call"]["container_id"] == "e" * 64


def test_reviewed_active_call_returns_to_running_only_after_exact_match(tmp_path):
    _, store, session = _create(tmp_path)
    labels = {"io.pico.call": "call-1"}
    store.begin_call(
        session.state_root,
        call_id="call-1",
        reconciliation_token="c" * 64,
        container_name="pico-call-1",
        expected_labels=labels,
        plan_digest="sha256:" + "d" * 64,
    )
    store.record_container_id(session.state_root, "e" * 64)
    store.finish_call(session.state_root, review_required=True)

    running = store.reconcile_active_call(
        session.state_root,
        lambda _active: [
            {
                "id": "e" * 64,
                "name": "pico-call-1",
                "labels": labels,
                "contract_matches": True,
            }
        ],
    )

    assert running.state == "running"
    assert store.finish_call(session.state_root).state == "ready"


def test_discard_removes_only_execution_root_and_preserves_manifest(tmp_path):
    source, store, session = _create(tmp_path)
    workspace = session.workspace_view.physical_root

    discarded = store.discard(session.state_root)

    assert discarded.state == "discarded"
    assert discarded.manifest["cleanup"]["status"] == "complete"
    assert not workspace.exists()
    assert (session.state_root / "manifest.json").exists()
    assert (source / "README.md").read_text() == "demo\n"


def test_discard_cleanup_is_bounded_and_resumable(tmp_path):
    _source, store, session = _create(tmp_path)
    workspace = session.workspace_view.physical_root

    pending = store.discard(session.state_root, max_delete_entries=1)

    assert pending.state == "cleanup_pending"
    assert pending.manifest["cleanup"]["status"] == "pending"
    assert pending.manifest["lease"] is None
    assert not workspace.exists()
    assert (session.state_root / "trash-workspace").exists()

    acquired = store.acquire(session.state_root)
    completed = store.resume_cleanup(
        acquired.state_root,
        max_delete_entries=100,
    )

    assert completed.state == "discarded"
    assert completed.manifest["cleanup"]["status"] == "complete"
    assert completed.manifest["lease"] is None
    assert not (session.state_root / "trash-workspace").exists()


def test_discard_removes_staging_blobs_but_preserves_audit_metadata(tmp_path):
    _source, store, session = _create(tmp_path)
    recovery = ensure_private_dir(session.state_root / "recovery")
    metadata = recovery / "diff.json"
    metadata.write_text('{"audit":true}\n', encoding="utf-8")
    metadata.chmod(0o600)
    checkpoints = CheckpointStore(recovery / ".pico" / "checkpoints")
    blob_ref = checkpoints.write_blob(b"sensitive-before-bytes")["blob_ref"]
    blob_path = checkpoints.blobs_dir / blob_ref[:2] / blob_ref
    assert blob_path.is_file()

    discarded = store.discard(session.state_root)

    assert discarded.state == "discarded"
    assert metadata.read_text(encoding="utf-8") == '{"audit":true}\n'
    assert not checkpoints.blobs_dir.exists()
    assert not (session.state_root / "trash-content-blobs").exists()
    assert (session.state_root / "cleanup-artifacts.json").is_file()


def test_staging_blob_cleanup_shares_budget_and_resumes_from_trash(tmp_path):
    _source, store, session = _create(tmp_path)
    checkpoints = CheckpointStore(
        session.state_root / "recovery" / ".pico" / "checkpoints"
    )
    checkpoints.write_blob(b"before")
    workspace = session.workspace_view.physical_root

    pending = store.discard(session.state_root, max_delete_entries=5)

    assert pending.state == "cleanup_pending"
    assert not workspace.exists()
    assert not checkpoints.blobs_dir.exists()
    assert (session.state_root / "trash-content-blobs").is_dir()

    acquired = store.acquire(session.state_root)
    completed = store.resume_cleanup(acquired.state_root, max_delete_entries=100)

    assert completed.state == "discarded"
    assert not (session.state_root / "trash-content-blobs").exists()


def test_discard_refuses_symlink_staging_blob_root_without_following(tmp_path):
    _source, store, session = _create(tmp_path)
    checkpoints = ensure_private_dir(
        session.state_root / "recovery" / ".pico" / "checkpoints"
    )
    outside = tmp_path / "outside-blobs"
    outside.mkdir()
    (outside / "keep").write_text("keep\n", encoding="utf-8")
    (checkpoints / "blobs").symlink_to(outside, target_is_directory=True)
    workspace = session.workspace_view.physical_root

    pending = store.discard(session.state_root)

    assert pending.state == "cleanup_pending"
    assert workspace.is_dir()
    assert (checkpoints / "blobs").is_symlink()
    assert (outside / "keep").read_text(encoding="utf-8") == "keep\n"


def test_discard_unlinks_workspace_symlink_without_following_target(tmp_path):
    _source, store, session = _create(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
    (session.workspace_view.physical_root / "outside-link").symlink_to(
        outside,
        target_is_directory=True,
    )

    discarded = store.discard(session.state_root)

    assert discarded.state == "discarded"
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep\n"
