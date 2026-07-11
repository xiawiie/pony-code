import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import pico.memory.block_store as block_store_module
from pico.memory.block_store import BlockStore
from pico.memory.refresher import MemoryRefresher
from pico.memory.retrieval import Retrieval
from pico.repo_map import RepoMap
from pico import repo_map as repo_map_module


def _memory_store(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    user.mkdir()
    return BlockStore(workspace, user), workspace


def test_memory_index_skips_file_over_per_file_byte_limit(tmp_path, monkeypatch):
    store, workspace = _memory_store(tmp_path)
    monkeypatch.setattr(
        block_store_module,
        "MAX_MEMORY_FILE_BYTES",
        32,
        raising=False,
    )
    (workspace / "notes" / "oversized.md").write_bytes(b"x" * 33)

    assert store.list() == []


def test_memory_index_stops_at_file_count_limit(tmp_path, monkeypatch):
    store, workspace = _memory_store(tmp_path)
    monkeypatch.setattr(
        block_store_module,
        "MAX_MEMORY_INDEX_FILES",
        2,
        raising=False,
    )
    for name in ("a.md", "b.md", "c.md"):
        (workspace / "notes" / name).write_text(name, encoding="utf-8")

    assert [entry.path for entry in store.list()] == [
        "workspace/notes/a.md",
        "workspace/notes/b.md",
    ]


def test_memory_file_count_includes_unsafe_candidates(tmp_path, monkeypatch):
    store, workspace = _memory_store(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "notes" / "a.md").symlink_to(outside)
    (workspace / "notes" / "b.md").write_text("safe", encoding="utf-8")
    monkeypatch.setattr(block_store_module, "MAX_MEMORY_INDEX_FILES", 1)

    assert store.list() == []


def test_memory_list_reads_each_safe_candidate_exactly_once(tmp_path, monkeypatch):
    store, workspace = _memory_store(tmp_path)
    for name in ("a.md", "b.md", "c.md"):
        (workspace / "notes" / name).write_text(name, encoding="utf-8")
    calls = []
    real_read = block_store_module._read_bounded_regular

    def counting_read(path, limit, *, private=False):
        calls.append(Path(path).name)
        return real_read(path, limit, private=private)

    monkeypatch.setattr(block_store_module, "_read_bounded_regular", counting_read)

    assert len(store.list()) == 3
    assert calls == ["a.md", "b.md", "c.md"]


def test_memory_index_stops_before_aggregate_byte_limit(tmp_path, monkeypatch):
    store, workspace = _memory_store(tmp_path)
    monkeypatch.setattr(
        block_store_module,
        "MAX_MEMORY_FILE_BYTES",
        32,
        raising=False,
    )
    monkeypatch.setattr(
        block_store_module,
        "MAX_MEMORY_INDEX_BYTES",
        9,
        raising=False,
    )
    (workspace / "notes" / "a.md").write_bytes(b"aaaaa")
    (workspace / "notes" / "b.md").write_bytes(b"bbbbb")

    assert [entry.path for entry in store.list()] == ["workspace/notes/a.md"]


def test_retrieval_preserves_aggregate_byte_limit(tmp_path, monkeypatch):
    store, workspace = _memory_store(tmp_path)
    monkeypatch.setattr(block_store_module, "MAX_MEMORY_FILE_BYTES", 32)
    monkeypatch.setattr(block_store_module, "MAX_MEMORY_INDEX_BYTES", 9)
    (workspace / "notes" / "a.md").write_bytes(b"aaaaa")
    (workspace / "notes" / "b.md").write_bytes(b"bbbbb")

    assert Retrieval(store).search("bbbbb") == []


def test_memory_aggregate_counts_bytes_read_while_detecting_growth(
    tmp_path,
    monkeypatch,
):
    store, workspace = _memory_store(tmp_path)
    for name in ("a.md", "b.md", "c.md"):
        (workspace / "notes" / name).write_text("safe", encoding="utf-8")
    monkeypatch.setattr(block_store_module, "MAX_MEMORY_FILE_BYTES", 4)
    monkeypatch.setattr(block_store_module, "MAX_MEMORY_INDEX_BYTES", 6)
    calls = []

    class GrewDuringRead(ValueError):
        def __init__(self, bytes_read):
            super().__init__("memory file too large")
            self.bytes_read = bytes_read

    def growing_read(_path, limit, *, private=False):
        calls.append((limit, private))
        raise GrewDuringRead(limit + 1)

    monkeypatch.setattr(block_store_module, "_read_bounded_regular", growing_read)

    assert store.list() == []
    assert calls == [(4, False), (1, False)]


def test_memory_read_rejects_oversized_full_body(tmp_path, monkeypatch):
    store, workspace = _memory_store(tmp_path)
    monkeypatch.setattr(
        block_store_module,
        "MAX_MEMORY_FILE_BYTES",
        8,
        raising=False,
    )
    (workspace / "notes" / "large.md").write_bytes(b"123456789")

    with pytest.raises(ValueError, match="memory file too large"):
        store.read("workspace/notes/large.md")


def test_memory_index_rejects_leaf_replaced_after_descriptor_open(
    tmp_path,
    monkeypatch,
):
    store, workspace = _memory_store(tmp_path)
    target = workspace / "notes" / "race.md"
    target.write_text("original\n", encoding="utf-8")
    real_open = os.open
    swapped = False

    def swap_after_open(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)
        if (
            not swapped
            and kwargs.get("dir_fd") is not None
            and Path(path).name == target.name
        ):
            swapped = True
            target.unlink()
            target.write_text("replacement\n", encoding="utf-8")
        return descriptor

    monkeypatch.setattr(block_store_module.os, "open", swap_after_open)

    assert store.list() == []
    assert swapped is True


def test_retrieval_fails_closed_on_descriptor_swap_then_refreshes_next_query(
    tmp_path,
    monkeypatch,
):
    store, workspace = _memory_store(tmp_path)
    target = workspace / "notes" / "race.md"
    target.write_text(
        "---\nname: original\ndescription: cache\n---\noriginal body\n",
        encoding="utf-8",
    )
    retrieval = Retrieval(store)
    real_open = os.open
    swapped = False

    def swap_after_open(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = real_open(path, flags, *args, **kwargs)
        if (
            not swapped
            and kwargs.get("dir_fd") is not None
            and Path(path).name == target.name
        ):
            swapped = True
            target.unlink()
            target.write_text(
                "---\nname: replacement\ndescription: cache\n---\n"
                "replacement body\n",
                encoding="utf-8",
            )
        return descriptor

    monkeypatch.setattr(block_store_module.os, "open", swap_after_open)

    assert retrieval.search("cache") == []
    hits = retrieval.search("replacement")
    assert [hit.path for hit in hits] == ["workspace/notes/race.md"]
    assert any("replacement" in snippet for snippet in hits[0].snippets)


def test_memory_refresher_lists_once_per_refresh():
    entry = SimpleEntry("workspace/notes/a.md", 1, 1.0)
    store = MagicMock()
    store.list.return_value = [entry]
    repo_map = MagicMock()
    repo_map.top_level_tree.return_value = []
    repo_map.language_stats.return_value = {}
    refresher = MemoryRefresher(store, repo_map)

    refresher.refresh_if_stale()

    assert store.list.call_count == 1


class SimpleEntry:
    def __init__(self, path, size_chars, mtime):
        self.path = path
        self.size_chars = size_chars
        self.mtime = mtime


def test_repo_map_uses_descriptor_reader(tmp_path, monkeypatch):
    target = tmp_path / "source.py"
    target.write_text("class DescriptorRead: pass\n", encoding="utf-8")

    def fail_path_read(*_args, **_kwargs):
        raise AssertionError("Path.read_text must not serve repo-map sources")

    monkeypatch.setattr(Path, "read_text", fail_path_read)
    repo_map = RepoMap(tmp_path)

    repo_map.scan()

    assert repo_map.lookup("DescriptorRead")


def test_repo_map_stops_before_aggregate_byte_limit(tmp_path, monkeypatch):
    source_a = "class First: pass\n"
    source_b = "class Second: pass\n"
    (tmp_path / "a.py").write_text(source_a, encoding="utf-8")
    (tmp_path / "b.py").write_text(source_b, encoding="utf-8")
    monkeypatch.setattr(
        repo_map_module,
        "MAX_TOTAL_BYTES",
        len(source_a.encode("utf-8")),
        raising=False,
    )
    repo_map = RepoMap(tmp_path)

    repo_map.scan()

    assert repo_map.lookup("First")
    assert repo_map.lookup("Second") == []


def test_repo_map_refresh_counts_unchanged_index_bytes(tmp_path, monkeypatch):
    source_a = "class First: pass\n"
    source_b = "class Second: pass\n"
    source_c = "class Third: pass\n"
    (tmp_path / "a.py").write_text(source_a, encoding="utf-8")
    (tmp_path / "b.py").write_text(source_b, encoding="utf-8")
    monkeypatch.setattr(
        repo_map_module,
        "MAX_TOTAL_BYTES",
        len(source_a.encode("utf-8")) + len(source_b.encode("utf-8")),
    )
    repo_map = RepoMap(tmp_path)
    repo_map.scan()
    (tmp_path / "c.py").write_text(source_c, encoding="utf-8")

    repo_map.refresh_if_stale()

    assert repo_map.lookup("First")
    assert repo_map.lookup("Second")
    assert repo_map.lookup("Third") == []


def test_repo_map_file_count_includes_unsafe_candidates(tmp_path, monkeypatch):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("class Outside: pass\n", encoding="utf-8")
    (tmp_path / "a.py").symlink_to(outside)
    (tmp_path / "b.py").write_text("class Safe: pass\n", encoding="utf-8")
    monkeypatch.setattr(repo_map_module, "MAX_FILES", 1)
    repo_map = RepoMap(tmp_path)

    repo_map.scan()

    assert repo_map.lookup("Safe") == []


def test_repo_map_aggregate_counts_bytes_read_while_detecting_growth(
    tmp_path,
    monkeypatch,
):
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("class Safe: pass\n", encoding="utf-8")
    monkeypatch.setattr(repo_map_module, "MAX_FILE_SIZE", 4)
    monkeypatch.setattr(repo_map_module, "MAX_TOTAL_BYTES", 6)
    calls = []

    class GrewDuringRead(ValueError):
        def __init__(self, bytes_read):
            super().__init__("repo-map source too large")
            self.bytes_read = bytes_read

    def growing_read(_path, limit):
        calls.append(limit)
        raise GrewDuringRead(limit + 1)

    monkeypatch.setattr(repo_map_module, "_read_bounded_regular", growing_read)
    repo_map = RepoMap(tmp_path)

    repo_map.scan()

    assert calls == [4, 1]


def test_repo_map_rechecks_size_on_open_descriptor(tmp_path, monkeypatch):
    target = tmp_path / "growing.py"
    target.write_text("class InitiallySmall: pass\n", encoding="utf-8")
    monkeypatch.setattr(repo_map_module, "MAX_FILE_SIZE", 64)
    real_open = os.open
    opened = False

    def grow_after_open(path, flags, *args, **kwargs):
        nonlocal opened
        descriptor = real_open(path, flags, *args, **kwargs)
        if (
            not opened
            and kwargs.get("dir_fd") is not None
            and Path(path).name == target.name
        ):
            opened = True
            with target.open("ab") as handle:
                handle.write(b"x" * 128)
        return descriptor

    monkeypatch.setattr(repo_map_module.os, "open", grow_after_open)
    repo_map = RepoMap(tmp_path)

    repo_map.scan()

    assert opened is True
    assert repo_map.lookup("InitiallySmall") == []
