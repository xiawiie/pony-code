from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path

import pytest

from pony.cli import assembly
from pony.cli.errors import CliError


class FakeTrustStore:
    def __init__(self, *, trusted=False, trust_succeeds=True, events=None):
        self.trusted = trusted
        self.trust_succeeds = trust_succeeds
        self.events = events if events is not None else []

    def is_trusted(self, project_root):
        self.events.append(("is_trusted", project_root))
        return self.trusted

    def trust(self, project_root):
        self.events.append(("trust", project_root))
        self.trusted = self.trust_succeeds


def _args(root, *, no_input=False):
    return SimpleNamespace(cwd=str(root), no_input=no_input)


def test_untrusted_no_input_stops_before_workspace_and_provider(tmp_path, monkeypatch):
    workspace = Mock()
    downstream = Mock()
    confirm = Mock(return_value=True)
    monkeypatch.setattr(assembly.WorkspaceContext, "build", workspace)
    monkeypatch.setattr(assembly, "_build_agent", downstream)

    with pytest.raises(CliError) as raised:
        assembly.build_agent(
            _args(tmp_path, no_input=True),
            trust_store=FakeTrustStore(),
            confirm=confirm,
        )

    assert raised.value.code == "project_untrusted"
    confirm.assert_not_called()
    workspace.assert_not_called()
    downstream.assert_not_called()


def test_default_store_no_input_rejection_does_not_create_home_state(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    workspace = Mock()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(assembly.WorkspaceContext, "build", workspace)

    with pytest.raises(CliError) as raised:
        assembly.build_agent(_args(project, no_input=True))

    assert raised.value.code == "project_untrusted"
    assert not (home / ".pony").exists()
    workspace.assert_not_called()


def test_rejected_trust_stops_before_workspace_and_provider(tmp_path, monkeypatch):
    workspace = Mock()
    downstream = Mock()
    monkeypatch.setattr(assembly.WorkspaceContext, "build", workspace)
    monkeypatch.setattr(assembly, "_build_agent", downstream)

    with pytest.raises(CliError) as raised:
        assembly.build_agent(
            _args(tmp_path),
            trust_store=FakeTrustStore(),
            confirm=lambda _root: False,
        )

    assert raised.value.code == "project_untrusted"
    workspace.assert_not_called()
    downstream.assert_not_called()


def test_trust_is_persisted_before_workspace_and_reused(tmp_path, monkeypatch):
    events = []
    store = FakeTrustStore(events=events)
    confirm = Mock(side_effect=lambda root: events.append(("confirm", root)) or True)
    workspace = SimpleNamespace(repo_root=str(tmp_path))

    def build_workspace(_cwd):
        events.append(("workspace", tmp_path))
        return workspace

    def downstream(_args, value):
        events.append(("downstream", tmp_path))
        assert value is workspace
        return "agent"

    monkeypatch.setattr(assembly.WorkspaceContext, "build", build_workspace)
    monkeypatch.setattr(assembly, "_build_agent", downstream)

    assert assembly.build_agent(
        _args(tmp_path), trust_store=store, confirm=confirm
    ) == "agent"
    assert [event[0] for event in events] == [
        "is_trusted",
        "confirm",
        "trust",
        "is_trusted",
        "workspace",
        "is_trusted",
        "downstream",
    ]

    events.clear()
    assert assembly.build_agent(
        _args(tmp_path), trust_store=store, confirm=confirm
    ) == "agent"
    confirm.assert_called_once()
    assert [event[0] for event in events] == [
        "is_trusted",
        "is_trusted",
        "workspace",
        "is_trusted",
        "downstream",
    ]


def test_identity_drift_after_trust_fails_before_workspace(tmp_path, monkeypatch):
    workspace = Mock()
    monkeypatch.setattr(assembly.WorkspaceContext, "build", workspace)

    with pytest.raises(CliError) as raised:
        assembly.build_agent(
            _args(tmp_path),
            trust_store=FakeTrustStore(trust_succeeds=False),
            confirm=lambda _root: True,
        )

    assert raised.value.code == "project_trust_changed"
    workspace.assert_not_called()


def test_identity_drift_during_confirmation_is_never_trusted(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    workspace = Mock()
    store = FakeTrustStore()
    monkeypatch.setattr(assembly.WorkspaceContext, "build", workspace)

    def replace_project(_root):
        project.rename(tmp_path / "old-project")
        project.mkdir()
        return True

    with pytest.raises(CliError) as raised:
        assembly.build_agent(
            _args(project),
            trust_store=store,
            confirm=replace_project,
        )

    assert raised.value.code == "project_trust_changed"
    assert not any(event[0] == "trust" for event in store.events)
    workspace.assert_not_called()
