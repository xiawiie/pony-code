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
