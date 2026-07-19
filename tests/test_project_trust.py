import json
import os
import stat

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
