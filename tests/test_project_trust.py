import json
import os
import stat
import threading

import pytest

from pony.security.trust import ProjectTrustStore


def test_project_trust_is_private_persistent_and_revocable(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    store = ProjectTrustStore(state)

    assert store.is_trusted(project) is False
    store.trust(project)

    assert ProjectTrustStore(state).is_trusted(project) is True
    if os.name == "posix":
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert stat.S_IMODE((state / "trust.json").stat().st_mode) == 0o600

    store.revoke(project)
    assert store.is_trusted(project) is False


def test_project_root_identity_drift_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = ProjectTrustStore(tmp_path / "state")
    store.trust(project)

    project.rename(tmp_path / "old-project")
    project.mkdir()

    assert store.is_trusted(project) is False


def test_revoke_waits_for_stale_writer_and_remains_final(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    store = ProjectTrustStore(tmp_path / "state")
    store.trust(project)
    stale_loaded = threading.Event()
    release_stale = threading.Event()
    revoke_started = threading.Event()
    revoke_finished = threading.Event()
    errors = []
    original_load = store._load_projects
    stale_thread = None

    def delayed_load():
        projects = original_load()
        if threading.current_thread() is stale_thread:
            stale_loaded.set()
            if not release_stale.wait(5):
                raise TimeoutError("stale writer was not released")
        return projects

    def stale_trust():
        try:
            store.trust(project)
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    def revoke():
        revoke_started.set()
        try:
            store.revoke(project)
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)
        finally:
            revoke_finished.set()

    monkeypatch.setattr(store, "_load_projects", delayed_load)
    stale_thread = threading.Thread(target=stale_trust)
    revoke_thread = threading.Thread(target=revoke)
    stale_thread.start()
    assert stale_loaded.wait(5)
    revoke_thread.start()
    assert revoke_started.wait(5)
    assert not revoke_finished.wait(0.1)
    release_stale.set()
    stale_thread.join(5)
    revoke_thread.join(5)

    assert not stale_thread.is_alive()
    assert not revoke_thread.is_alive()
    assert errors == []
    assert store.is_trusted(project) is False


def test_project_trust_refuses_symlink_root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(project, target_is_directory=True)
    store = ProjectTrustStore(tmp_path / "state")

    with pytest.raises(ValueError, match="symlink"):
        store.trust(linked)
    assert store.is_trusted(linked) is False


def test_corrupt_trust_store_denies_without_overwriting(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    store = ProjectTrustStore(state)
    store.trust(project)
    path = state / "trust.json"
    path.write_text(json.dumps({"version": 1, "projects": []}), encoding="utf-8")
    path.chmod(0o600)

    assert store.is_trusted(project) is False
    with pytest.raises(ValueError, match="invalid trust store"):
        store.trust(project)
