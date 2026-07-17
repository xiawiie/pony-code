import os
from pathlib import Path
from unittest.mock import patch

from pony import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from benchmarks.support.fake_provider import FakeModelClient
from pony.sandbox.session import source_apply_control_lock_path
from pony.runtime.options import RuntimeOptions


def test_pony_control_lock_stays_in_isolated_home(tmp_path, isolated_home):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(workspace),
        session_store=SessionStore(workspace / ".pony" / "sessions"),
        options=RuntimeOptions(approval_policy="auto"),
    )

    with agent.checkpoint_store.mutation_lock():
        pass

    expected = source_apply_control_lock_path(
        isolated_home / ".pony" / "sandboxes",
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
