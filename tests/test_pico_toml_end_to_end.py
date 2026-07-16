"""A Pico instance consumes one immutable project-config snapshot."""

import pico.config as config
from pico import Pico, SessionStore, WorkspaceContext
from pico.config import load_pico_toml
from pico.providers.fake import FakeModelClient


PICO_TOML = """
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


def test_full_pico_toml_overrides_take_effect(tmp_path):
    (tmp_path / "pico.toml").write_text(PICO_TOML, encoding="utf-8")
    cfg = load_pico_toml(tmp_path)

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


def test_partial_pico_toml_only_overrides_provided_keys(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[memory.recall]\nmin_score = 0.7\n", encoding="utf-8"
    )
    cfg = load_pico_toml(tmp_path)
    assert cfg["context"]["source_pool_tokens"] == 16_384
    assert cfg["memory"]["recall"]["min_score"] == 0.7
    assert cfg["memory"]["recall"]["top_k"] == 6


def test_pico_parses_once_per_instance_without_cross_instance_cache(
    tmp_path,
    monkeypatch,
):
    toml_path = tmp_path / "pico.toml"
    toml_path.write_text(PICO_TOML, encoding="utf-8")
    real_load = config.tomllib.load
    parse_count = 0

    def counting_load(file):
        nonlocal parse_count
        parse_count += 1
        return real_load(file)

    monkeypatch.setattr(config.tomllib, "load", counting_load)
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    first = Pico(FakeModelClient([]), workspace, store)

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
        PICO_TOML.replace("source_pool_tokens = 10000", "source_pool_tokens = 12000"),
        encoding="utf-8",
    )
    second = Pico(FakeModelClient([]), workspace, store)

    assert parse_count == 2
    assert first.context_config["source_pool_tokens"] == 10_000
    assert second.context_config["source_pool_tokens"] == 12_000
