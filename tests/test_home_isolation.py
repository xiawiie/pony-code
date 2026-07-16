import os
from pathlib import Path
from unittest.mock import patch

from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient
from pico.sandbox.session import source_apply_control_lock_path


def test_pico_control_lock_stays_in_isolated_home(tmp_path, isolated_home):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(workspace),
        session_store=SessionStore(workspace / ".pico" / "sessions"),
        approval_policy="auto",
    )

    with agent.checkpoint_store.mutation_lock():
        pass

    expected = source_apply_control_lock_path(
        isolated_home / ".pico" / "sandboxes",
        workspace,
    )
    assert Path.home() == isolated_home
    assert expected.is_file()


def test_explicit_home_override_remains_visible(tmp_path, isolated_home):
    explicit_home = tmp_path / "explicit-home"
    explicit_home.mkdir()

    with patch.dict(os.environ, {"HOME": str(explicit_home)}, clear=True):
        assert Path.home() == explicit_home
    with patch.dict(os.environ, {}, clear=True):
        assert Path.home() == isolated_home
