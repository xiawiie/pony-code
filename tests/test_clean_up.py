"""Task 8 cleanup: obsolete constructs must be gone.

- `pony/working_memory.py` module is deleted (no producer/consumer since Task 5+).
- `feature_flags["relevant_memory"]` is no longer a default flag (never consumed).
- Prompt metadata carries a single cache-key field (`system_prefix_hash`);
  the three synonymous hashes (`base_prefix_hash` / `stable_prefix_hash` /
  `prefix_hash` / `prompt_cache_key`) are gone.
"""

import importlib

import pytest
from pony.runtime.options import RuntimeOptions


def test_no_working_memory_module():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("pony.working_memory")


def test_default_feature_flags_no_relevant_memory():
    from pony.runtime.application import DEFAULT_FEATURE_FLAGS

    assert "relevant_memory" not in DEFAULT_FEATURE_FLAGS


def test_metadata_uses_system_prefix_hash_only(tmp_path):
    from pony import Pony
    from pony.state.session_store import SessionStore
    from pony.workspace.context import WorkspaceContext
    from benchmarks.support.fake_provider import FakeModelClient
    from pony.context.renderer import render_current_user_message

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(approval_policy="auto"),
    )

    agent.session["messages"].append(
        {
            "role": "user",
            "content": "x",
            "_pony_meta": {"created_at": "2026-07-10T00:00:00+00:00"},
        }
    )
    snapshot, telemetry = render_current_user_message(agent, "x")
    _, metadata = agent.context_manager.build_request(
        injection_snapshot=snapshot,
        injection_telemetry=telemetry,
        preflight_metadata={},
    )

    assert "system_prefix_hash" in metadata
    assert "prompt_cache_key" not in metadata
    assert "base_prefix_hash" not in metadata
    assert "stable_prefix_hash" not in metadata
    assert "prefix_hash" not in metadata
