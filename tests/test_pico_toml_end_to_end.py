"""A Pico instance consumes one immutable project-config snapshot."""

import pico.config as config
from pico import Pico, SessionStore, WorkspaceContext
from pico.providers.fake import FakeModelClient
from pico.config import load_pico_toml


PICO_TOML = """
[policy]
max_blob_size = 4096

[context]
history_soft_cap = 12345
history_floor_messages = 8
injection_budget_ratio = 0.25
system_tools_hard_cap = 30000
total_budget_hard_cap = 90000

[context.digest]
size_threshold_chars = 500

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
    assert cfg["context"] == {
        "history_soft_cap": 12345,
        "history_floor_messages": 8,
        "injection_budget_ratio": 0.25,
        "system_tools_hard_cap": 30000,
        "total_budget_hard_cap": 90000,
        "digest": {"size_threshold_chars": 500},
    }
    assert cfg["memory"]["recall"] == {
        "min_score": 0.45,
        "top_k": 3,
        "max_tokens_per_note": 300,
        "skip_recent_turns": 4,
    }
    assert cfg["memory"]["retrieval"]["field_boost"] == {
        "name": 6.0,
        "description": 3.5,
        "tags": 4.5,
        "aliases": 4.0,
        "body": 1.5,
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
    assert cfg["context"]["history_soft_cap"] == 40000
    recall = cfg["memory"]["recall"]
    assert recall["min_score"] == 0.7
    assert recall["top_k"] == 2


def test_pico_parses_once_per_instance_without_cross_instance_cache(
    tmp_path, monkeypatch
):
    toml_path = tmp_path / "pico.toml"
    toml_path.write_text(PICO_TOML, encoding="utf-8")
    real_loads = config.tomllib.loads
    parse_count = 0

    def counting_loads(text):
        nonlocal parse_count
        parse_count += 1
        return real_loads(text)

    monkeypatch.setattr(config.tomllib, "loads", counting_loads)
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    first = Pico(FakeModelClient([]), workspace, store)
    assert parse_count == 1
    assert first.project_max_blob_size == 4096
    assert first.context_config["history_soft_cap"] == 12345
    assert first.context_config["digest_size_threshold"] == 500
    assert first.context_config["recall"]["top_k"] == 3
    assert first.context_config["field_boosts"]["name"] == 6.0
    assert first.context_config["link_config"] == (5, 0.5)

    toml_path.write_text(
        PICO_TOML.replace("history_soft_cap = 12345", "history_soft_cap = 54321"),
        encoding="utf-8",
    )
    second = Pico(FakeModelClient([]), workspace, store)

    assert parse_count == 2
    assert first.context_config["history_soft_cap"] == 12345
    assert second.context_config["history_soft_cap"] == 54321
