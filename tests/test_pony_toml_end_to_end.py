"""A Pony instance consumes one immutable project-config snapshot."""

import pony.config.project as config
from pony import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from pony.config.project import load_pony_toml
from benchmarks.support.fake_provider import FakeModelClient


PONY_TOML = """
[policy]
max_blob_size = 4096

[model]
context_window = 90000
output_limit = 12000

[context]
system_tools_hard_cap = 18000
source_pool_tokens = 10000

[context.compaction]
reserve_tokens = 14000
keep_recent_tokens = 18000

[context.tool_results]
inline_tokens = 2048
digest_tokens = 384

[memory.recall]
min_score = 0.45
top_k = 3
max_tokens_per_note = 300
skip_recent_turns = 4

[memory.retrieval.field_boost]
name = 6.0
description = 3.5
tags = 4.5
aliases = 4.0
body = 1.5

[memory.retrieval.link]
max_added = 5
decay = 0.5
"""


def test_full_pony_toml_overrides_take_effect(tmp_path):
    (tmp_path / "pony.toml").write_text(PONY_TOML, encoding="utf-8")
    cfg = load_pony_toml(tmp_path)

    assert cfg["policy"]["max_blob_size"] == 4096
    assert cfg["model"] == {"context_window": 90_000, "output_limit": 12_000}
    assert cfg["context"] == {
        "system_tools_hard_cap": 18_000,
        "source_pool_tokens": 10_000,
        "compaction": {
            "enabled": True,
            "reserve_tokens": 14_000,
            "keep_recent_tokens": 18_000,
        },
        "tool_results": {"inline_tokens": 2_048, "digest_tokens": 384},
    }
    assert cfg["memory"]["recall"] == {
        "min_score": 0.45,
        "top_k": 3,
        "max_tokens_per_note": 300,
        "skip_recent_turns": 4,
    }
    assert cfg["memory"]["retrieval"]["link"] == {
        "max_added": 5,
        "decay": 0.5,
    }


def test_partial_pony_toml_only_overrides_provided_keys(tmp_path):
    (tmp_path / "pony.toml").write_text(
        "[memory.recall]\nmin_score = 0.7\n", encoding="utf-8"
    )
    cfg = load_pony_toml(tmp_path)
    assert cfg["context"]["source_pool_tokens"] == 16_384
    assert cfg["memory"]["recall"]["min_score"] == 0.7
    assert cfg["memory"]["recall"]["top_k"] == 6


def test_pony_parses_once_per_instance_without_cross_instance_cache(
    tmp_path,
    monkeypatch,
):
    toml_path = tmp_path / "pony.toml"
    toml_path.write_text(PONY_TOML, encoding="utf-8")
    real_loads = config.tomllib.loads
    parse_count = 0

    def counting_loads(text):
        nonlocal parse_count
        parse_count += 1
        return real_loads(text)

    monkeypatch.setattr(config.tomllib, "loads", counting_loads)
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    first = Pony(FakeModelClient([]), workspace, store)

    assert parse_count == 1
    assert first.project_max_blob_size == 4096
    assert first.model_capabilities.context_window == 90_000
    assert first.max_output_tokens == 12_000
    assert first.model_budget.reserve_tokens == 14_000
    assert first.context_config["source_pool_tokens"] == 10_000
    assert first.context_config["tool_results"]["inline_tokens"] == 2_048
    assert first.context_config["recall"]["top_k"] == 3
    assert first.context_config["field_boosts"]["name"] == 6.0
    assert first.context_config["link_config"] == (5, 0.5)

    toml_path.write_text(
        PONY_TOML.replace("source_pool_tokens = 10000", "source_pool_tokens = 12000"),
        encoding="utf-8",
    )
    second = Pony(FakeModelClient([]), workspace, store)

    assert parse_count == 2
    assert first.context_config["source_pool_tokens"] == 10_000
    assert second.context_config["source_pool_tokens"] == 12_000
