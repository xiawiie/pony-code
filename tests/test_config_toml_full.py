"""The current pico.toml loader is strict, complete, and content-free on errors."""

import pico.config as config
from pico.config import load_pico_toml


def test_reads_flat_scalars(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[context]\nhistory_soft_cap = 12345\n", encoding="utf-8"
    )
    data = load_pico_toml(tmp_path)
    assert data["context"]["history_soft_cap"] == 12345


def test_reads_nested_tables(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[memory.retrieval.field_boost]\nname = 5.5\ndescription = 3.5\n",
        encoding="utf-8",
    )
    data = load_pico_toml(tmp_path)
    assert data["memory"]["retrieval"]["field_boost"]["name"] == 5.5
    assert data["memory"]["retrieval"]["field_boost"]["description"] == 3.5


def test_ignores_unknown_fields(tmp_path):
    (tmp_path / "pico.toml").write_text(
        '[test]\nkeywords = ["a", "b", "c"]\n', encoding="utf-8"
    )
    data = load_pico_toml(tmp_path)
    assert "test" not in data


def test_returns_complete_defaults_when_file_missing(tmp_path):
    data = load_pico_toml(tmp_path)
    assert data["policy"]["max_blob_size"] == 8 * 1024 * 1024
    assert data["context"]["history_soft_cap"] == 40000
    assert data["memory"]["recall"]["top_k"] == 2


def test_malformed_uses_defaults_without_echoing_content(tmp_path, capsys):
    secret = "sk-secret-shaped-canary"
    (tmp_path / "pico.toml").write_text(
        f'PICO_OPENAI_API_KEY = "{secret}"\n[[[[not toml\n', encoding="utf-8"
    )

    data = load_pico_toml(tmp_path)

    assert data["context"]["history_soft_cap"] == 40000
    error = capsys.readouterr().err
    assert error == "warning: invalid pico.toml; using defaults\n"
    assert secret not in error


def test_non_table_uses_defaults(monkeypatch, tmp_path, capsys):
    (tmp_path / "pico.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(config.tomllib, "loads", lambda _text: [])

    data = load_pico_toml(tmp_path)

    assert data["context"]["history_soft_cap"] == 40000
    assert capsys.readouterr().err == "warning: invalid pico.toml; using defaults\n"


def test_invalid_fields_fall_back_independently(tmp_path):
    (tmp_path / "pico.toml").write_text(
        """
[policy]
max_blob_size = true

[context]
history_soft_cap = 12345
history_floor_messages = 0
injection_budget_ratio = inf

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

    data = load_pico_toml(tmp_path)

    assert data["policy"]["max_blob_size"] == 8 * 1024 * 1024
    assert data["context"]["history_soft_cap"] == 12345
    assert data["context"]["history_floor_messages"] == 6
    assert data["context"]["injection_budget_ratio"] == 0.15
    assert data["memory"]["recall"] == {
        "min_score": 0.7,
        "top_k": 2,
        "max_tokens_per_note": 400,
        "skip_recent_turns": 2,
    }
    assert data["memory"]["retrieval"]["field_boost"]["name"] == 8.0
    assert data["memory"]["retrieval"]["field_boost"]["body"] == 1.0
    assert data["memory"]["retrieval"]["link"] == {
        "max_added": 3,
        "decay": 0.4,
    }


def test_out_of_range_fields_warn_and_fall_back_independently(tmp_path, capsys):
    (tmp_path / "pico.toml").write_text(
        """
[policy]
max_blob_size = 8388609

[context]
history_soft_cap = 200001
history_floor_messages = 101
injection_budget_ratio = 0.5001
system_tools_hard_cap = 100001
total_budget_hard_cap = 200001

[context.digest]
size_threshold_chars = 1000001

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

    data = load_pico_toml(tmp_path)

    assert data["policy"]["max_blob_size"] == 8 * 1024 * 1024
    assert data["context"] == {
        "history_soft_cap": 40000,
        "history_floor_messages": 6,
        "injection_budget_ratio": 0.15,
        "system_tools_hard_cap": 20000,
        "total_budget_hard_cap": 100000,
        "digest": {"size_threshold_chars": 1200},
    }
    assert data["memory"]["recall"] == {
        "min_score": 0.3,
        "top_k": 2,
        "max_tokens_per_note": 400,
        "skip_recent_turns": 2,
    }
    assert data["memory"]["retrieval"]["field_boost"]["name"] == 5.0
    assert data["memory"]["retrieval"]["link"] == {
        "max_added": 3,
        "decay": 0.4,
    }
    warnings = capsys.readouterr().err
    for path in (
        "policy.max_blob_size",
        "context.history_soft_cap",
        "context.history_floor_messages",
        "context.injection_budget_ratio",
        "context.system_tools_hard_cap",
        "context.total_budget_hard_cap",
        "context.digest.size_threshold_chars",
        "memory.recall.min_score",
        "memory.recall.top_k",
        "memory.recall.max_tokens_per_note",
        "memory.recall.skip_recent_turns",
        "memory.retrieval.field_boost.name",
        "memory.retrieval.link.max_added",
        "memory.retrieval.link.decay",
    ):
        assert f"invalid pico.toml field {path}; using default" in warnings


def test_zero_valued_fields_are_accepted_where_documented(tmp_path, capsys):
    (tmp_path / "pico.toml").write_text(
        """
[context]
injection_budget_ratio = 0

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

    data = load_pico_toml(tmp_path)

    assert data["context"]["injection_budget_ratio"] == 0
    assert data["memory"]["recall"]["min_score"] == 0
    assert data["memory"]["recall"]["skip_recent_turns"] == 0
    assert data["memory"]["retrieval"]["field_boost"]["body"] == 0
    assert data["memory"]["retrieval"]["link"] == {
        "max_added": 0,
        "decay": 0,
    }
    assert capsys.readouterr().err == ""


def test_documented_upper_bounds_are_inclusive(tmp_path, capsys):
    (tmp_path / "pico.toml").write_text(
        """
[policy]
max_blob_size = 8388608

[context]
history_soft_cap = 200000
history_floor_messages = 100
injection_budget_ratio = 0.5
system_tools_hard_cap = 100000
total_budget_hard_cap = 200000

[context.digest]
size_threshold_chars = 1000000

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

    data = load_pico_toml(tmp_path)

    assert data["policy"]["max_blob_size"] == 8 * 1024 * 1024
    assert data["context"]["history_soft_cap"] == 200000
    assert data["context"]["history_floor_messages"] == 100
    assert data["context"]["injection_budget_ratio"] == 0.5
    assert data["context"]["system_tools_hard_cap"] == 100000
    assert data["context"]["total_budget_hard_cap"] == 200000
    assert data["context"]["digest"]["size_threshold_chars"] == 1000000
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


def test_context_resource_cap_relation_falls_back_as_one_group(tmp_path, capsys):
    (tmp_path / "pico.toml").write_text(
        """
[context]
system_tools_hard_cap = 50000
total_budget_hard_cap = 4096
history_soft_cap = 12345
""",
        encoding="utf-8",
    )

    context = load_pico_toml(tmp_path)["context"]

    assert context["system_tools_hard_cap"] == 20000
    assert context["total_budget_hard_cap"] == 100000
    assert context["history_soft_cap"] == 12345
    assert capsys.readouterr().err == (
        "warning: invalid pico.toml context resource caps; using defaults\n"
    )
