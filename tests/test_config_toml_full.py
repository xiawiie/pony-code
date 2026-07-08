"""Task B1: load_pico_toml_full prefers tomllib and supports arrays / nested tables."""

from pico.config import load_pico_toml_full


def test_reads_flat_scalars(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[context]\nhistory_soft_cap = 12345\n", encoding="utf-8"
    )
    data = load_pico_toml_full(tmp_path)
    assert data["context"]["history_soft_cap"] == 12345


def test_reads_nested_tables(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[memory.retrieval.field_boost]\nname = 5.5\ndescription = 3.5\n",
        encoding="utf-8",
    )
    data = load_pico_toml_full(tmp_path)
    assert data["memory"]["retrieval"]["field_boost"]["name"] == 5.5
    assert data["memory"]["retrieval"]["field_boost"]["description"] == 3.5


def test_reads_arrays(tmp_path):
    (tmp_path / "pico.toml").write_text(
        '[test]\nkeywords = ["a", "b", "c"]\n', encoding="utf-8"
    )
    data = load_pico_toml_full(tmp_path)
    assert data["test"]["keywords"] == ["a", "b", "c"]


def test_returns_empty_when_file_missing(tmp_path):
    data = load_pico_toml_full(tmp_path)
    assert data == {}


def test_returns_empty_when_malformed(tmp_path):
    (tmp_path / "pico.toml").write_text("[[[[not toml\n", encoding="utf-8")
    data = load_pico_toml_full(tmp_path)
    # Falls back to simple parser or returns empty; must NOT raise.
    assert isinstance(data, dict)
