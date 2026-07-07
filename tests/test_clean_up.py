"""Task 8 cleanup: obsolete constructs must be gone.

- `pico/working_memory.py` module is deleted (no producer/consumer since Task 5+).
- `feature_flags["relevant_memory"]` is no longer a default flag (never consumed).
- Prompt metadata carries a single cache-key field (`system_cache_key`);
  the three synonymous hashes (`base_prefix_hash` / `stable_prefix_hash` /
  `prefix_hash`) are gone. `prompt_cache_key` is kept as a one-release alias
  so existing provider adapters do not break.
"""

import importlib

import pytest


def test_no_working_memory_module():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("pico.working_memory")


def test_default_feature_flags_no_relevant_memory():
    from pico.runtime import DEFAULT_FEATURE_FLAGS

    assert "relevant_memory" not in DEFAULT_FEATURE_FLAGS


def test_metadata_uses_system_cache_key_only(tmp_path):
    from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )

    _, metadata = agent.context_manager.build_v2("x")

    assert "system_cache_key" in metadata
    assert "prompt_cache_key" in metadata  # kept as alias for one release
    assert "base_prefix_hash" not in metadata
    assert "stable_prefix_hash" not in metadata
    assert "prefix_hash" not in metadata

    _, build_metadata = agent.context_manager.build("x")
    assert "system_cache_key" in build_metadata
    assert "base_prefix_hash" not in build_metadata
    assert "stable_prefix_hash" not in build_metadata
    assert "prefix_hash" not in build_metadata
