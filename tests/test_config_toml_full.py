"""The pony.toml loader is strict, complete, and content-free on errors."""

import pony.config.project as config
from pony.config.project import load_pony_toml


def test_reads_flat_and_nested_budget_tables(tmp_path):
    (tmp_path / "pony.toml").write_text(
        "[model]\ncontext_window = 272000\n"
        "[context.compaction]\nreserve_tokens = 32000\n",
        encoding="utf-8",
    )
    data = load_pony_toml(tmp_path)

    assert data["model"]["context_window"] == 272_000
    assert data["context"]["compaction"]["reserve_tokens"] == 32_000


def test_reads_memory_nested_tables(tmp_path):
    (tmp_path / "pony.toml").write_text(
        "[memory.retrieval.field_boost]\nname = 5.5\ndescription = 3.5\n",
        encoding="utf-8",
    )
    data = load_pony_toml(tmp_path)
    assert data["memory"]["retrieval"]["field_boost"]["name"] == 5.5
    assert data["memory"]["retrieval"]["field_boost"]["description"] == 3.5


def test_ignores_unknown_fields(tmp_path):
    (tmp_path / "pony.toml").write_text(
        '[test]\nkeywords = ["a", "b", "c"]\n', encoding="utf-8"
    )
    assert "test" not in load_pony_toml(tmp_path)


def test_returns_complete_defaults_when_file_missing(tmp_path):
    data = load_pony_toml(tmp_path)
    assert data["model"] == {
        "context_window": 128_000,
        "output_limit": 16_384,
    }
    assert data["context"]["source_pool_tokens"] == 16_384
    assert data["memory"]["recall"]["top_k"] == 6


def test_malformed_uses_defaults_without_echoing_content(tmp_path, capsys):
    secret = "sk-secret-shaped-canary"
    (tmp_path / "pony.toml").write_text(
        f'PONY_OPENAI_API_KEY = "{secret}"\n[[[[not toml\n', encoding="utf-8"
    )

    data = load_pony_toml(tmp_path)

    assert data["model"]["context_window"] == 128_000
    error = capsys.readouterr().err
    assert error == "warning: invalid pony.toml; using defaults\n"
    assert secret not in error


def test_non_table_uses_defaults(monkeypatch, tmp_path, capsys):
    (tmp_path / "pony.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(config.tomllib, "loads", lambda _text: [])

    data = load_pony_toml(tmp_path)

    assert data["context"]["source_pool_tokens"] == 16_384
    assert capsys.readouterr().err == "warning: invalid pony.toml; using defaults\n"


def test_invalid_fields_fall_back_independently(tmp_path):
    (tmp_path / "pony.toml").write_text(
        """
[model]
context_window = -1
output_limit = 8192

[context]
source_pool_tokens = 0

[context.compaction]
enabled = "yes"
reserve_tokens = 32000

[memory.recall]
min_score = 0.7
top_k = -1

[memory.retrieval.field_boost]
name = 8.0
body = -1

[memory.retrieval.link]
max_added = true
decay = 2.0
""",
        encoding="utf-8",
    )

    data = load_pony_toml(tmp_path)

    assert data["model"] == {
        "context_window": 128_000,
        "output_limit": 8_192,
    }
    assert data["context"]["source_pool_tokens"] == 16_384
    assert data["context"]["compaction"]["enabled"] is True
    assert data["context"]["compaction"]["reserve_tokens"] == 32_000
    assert data["memory"]["recall"] == {
        "min_score": 0.7,
        "top_k": 6,
        "max_tokens_per_note": 1_024,
        "skip_recent_turns": 2,
    }
    assert data["memory"]["retrieval"]["field_boost"]["name"] == 8.0
    assert data["memory"]["retrieval"]["field_boost"]["body"] == 1.0
    assert data["memory"]["retrieval"]["link"] == {
        "max_added": 3,
        "decay": 0.4,
    }


def test_out_of_range_fields_warn_and_fall_back_independently(tmp_path, capsys):
    (tmp_path / "pony.toml").write_text(
        """
[model]
context_window = 2000001
output_limit = 384001

[context]
system_tools_hard_cap = 100001
source_pool_tokens = 200001

[context.compaction]
enabled = "yes"
reserve_tokens = 1000001
keep_recent_tokens = 1000001

[context.tool_results]
inline_tokens = 100001
digest_tokens = 16385

[memory.recall]
min_score = 1.1
top_k = 21
max_tokens_per_note = 4001
skip_recent_turns = 101

[memory.retrieval.field_boost]
name = 10.1

[memory.retrieval.link]
max_added = 21
decay = 1.1
""",
        encoding="utf-8",
    )

    data = load_pony_toml(tmp_path)

    assert data["model"] == {
        "context_window": 128000,
        "output_limit": 16384,
    }
    assert data["context"] == {
        "system_tools_hard_cap": 24576,
        "source_pool_tokens": 16384,
        "compaction": {
            "enabled": True,
            "reserve_tokens": 16384,
            "keep_recent_tokens": 20000,
        },
        "tool_results": {
            "inline_tokens": 4096,
            "digest_tokens": 512,
        },
    }
    assert data["memory"]["recall"] == {
        "min_score": 0.3,
        "top_k": 6,
        "max_tokens_per_note": 1024,
        "skip_recent_turns": 2,
    }
    assert data["memory"]["retrieval"]["field_boost"]["name"] == 5.0
    assert data["memory"]["retrieval"]["link"] == {
        "max_added": 3,
        "decay": 0.4,
    }
    warnings = capsys.readouterr().err
    for path in (
        "model.context_window",
        "model.output_limit",
        "context.system_tools_hard_cap",
        "context.source_pool_tokens",
        "context.compaction.enabled",
        "context.compaction.reserve_tokens",
        "context.compaction.keep_recent_tokens",
        "context.tool_results.inline_tokens",
        "context.tool_results.digest_tokens",
        "memory.recall.min_score",
        "memory.recall.top_k",
        "memory.recall.max_tokens_per_note",
        "memory.recall.skip_recent_turns",
        "memory.retrieval.field_boost.name",
        "memory.retrieval.link.max_added",
        "memory.retrieval.link.decay",
    ):
        assert f"invalid pony.toml field {path}; using default" in warnings


def test_zero_valued_fields_are_accepted_where_documented(tmp_path, capsys):
    (tmp_path / "pony.toml").write_text(
        """
[memory.recall]
min_score = 0
skip_recent_turns = 0

[memory.retrieval.field_boost]
body = 0

[memory.retrieval.link]
max_added = 0
decay = 0
""",
        encoding="utf-8",
    )

    data = load_pony_toml(tmp_path)

    assert data["memory"]["recall"]["min_score"] == 0
    assert data["memory"]["recall"]["skip_recent_turns"] == 0
    assert data["memory"]["retrieval"]["field_boost"]["body"] == 0
    assert data["memory"]["retrieval"]["link"] == {
        "max_added": 0,
        "decay": 0,
    }
    assert capsys.readouterr().err == ""


def test_documented_upper_bounds_are_inclusive(tmp_path, capsys):
    (tmp_path / "pony.toml").write_text(
        """
[model]
context_window = 2000000
output_limit = 384000

[context]
system_tools_hard_cap = 100000
source_pool_tokens = 200000

[context.compaction]
reserve_tokens = 1000000
keep_recent_tokens = 1000000

[context.tool_results]
inline_tokens = 100000
digest_tokens = 16384

[memory.recall]
min_score = 1
top_k = 20
max_tokens_per_note = 4000
skip_recent_turns = 100

[memory.retrieval.field_boost]
name = 10

[memory.retrieval.link]
max_added = 20
decay = 1
""",
        encoding="utf-8",
    )

    data = load_pony_toml(tmp_path)

    assert data["model"] == {
        "context_window": 2_000_000,
        "output_limit": 384_000,
    }
    assert data["context"]["system_tools_hard_cap"] == 100000
    assert data["context"]["source_pool_tokens"] == 200000
    assert data["context"]["compaction"] == {
        "enabled": True,
        "reserve_tokens": 1_000_000,
        "keep_recent_tokens": 1_000_000,
    }
    assert data["context"]["tool_results"] == {
        "inline_tokens": 100000,
        "digest_tokens": 16384,
    }
    assert data["memory"]["recall"] == {
        "min_score": 1.0,
        "top_k": 20,
        "max_tokens_per_note": 4000,
        "skip_recent_turns": 100,
    }
    assert data["memory"]["retrieval"]["field_boost"]["name"] == 10.0
    assert data["memory"]["retrieval"]["link"] == {
        "max_added": 20,
        "decay": 1.0,
    }
    assert capsys.readouterr().err == ""


def test_deprecated_total_budget_maps_to_model_context_independently(tmp_path, capsys):
    (tmp_path / "pony.toml").write_text(
        """
[context]
system_tools_hard_cap = 50000
total_budget_hard_cap = 4096
history_soft_cap = 12345
""",
        encoding="utf-8",
    )

    data = load_pony_toml(tmp_path)

    assert data["model"]["context_window"] == 4096
    assert data["context"]["system_tools_hard_cap"] == 50000
    assert "history_soft_cap" not in data["context"]
    assert capsys.readouterr().err == (
        "warning: [context].history_soft_cap was removed; use automatic compaction\n"
        "warning: [context].total_budget_hard_cap is deprecated; "
        "migrating it to [model].context_window\n"
    )
