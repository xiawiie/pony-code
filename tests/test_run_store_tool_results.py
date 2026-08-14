import hashlib
import os
from types import SimpleNamespace

import pytest

from pony.state.run_store import (
    MAX_RUN_TOOL_RESULT_BYTES,
    MAX_RUN_TOOL_RESULT_FILES,
    MAX_TOOL_RESULT_BYTES,
    RunStore,
    ToolResultStoreError,
)


def _hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _task(run_id="run1", status="running"):
    return SimpleNamespace(run_id=run_id, status=status)


def test_tool_result_round_trip_requires_full_hash_and_running_task(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    content = "safe output\n"
    content_hash = _hash(content)

    store.write_tool_result(_task(), content_hash, content)

    assert store.read_tool_result(_task(), f"tool_result:{content_hash}") == content
    with pytest.raises(ToolResultStoreError, match="tool_result_unavailable"):
        store.read_tool_result(_task(), f"tool_result:{content_hash[:16]}")
    with pytest.raises(ToolResultStoreError, match="tool_result_expired"):
        store.read_tool_result(
            _task(status="completed"),
            f"tool_result:{content_hash}",
        )


def test_tool_result_rejects_hash_mismatch(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")

    with pytest.raises(ToolResultStoreError, match="tool_result_retention_failed"):
        store.write_tool_result(_task(), "0" * 64, "different")


def test_tool_result_enforces_four_and_eight_mib_limits(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    first = "a" * MAX_TOOL_RESULT_BYTES
    second = "b" * MAX_TOOL_RESULT_BYTES

    store.write_tool_result(_task(), _hash(first), first)
    store.write_tool_result(_task(), _hash(second), second)

    assert MAX_RUN_TOOL_RESULT_BYTES == 2 * MAX_TOOL_RESULT_BYTES
    with pytest.raises(ToolResultStoreError, match="tool_result_retention_failed"):
        oversized = "x" * (MAX_TOOL_RESULT_BYTES + 1)
        store.write_tool_result(_task("run2"), _hash(oversized), oversized)
    with pytest.raises(ToolResultStoreError, match="tool_result_retention_failed"):
        store.write_tool_result(_task(), _hash("c"), "c")


def test_tool_result_enforces_one_hundred_distinct_files_without_eviction(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    for number in range(MAX_RUN_TOOL_RESULT_FILES):
        content = f"result {number}"
        store.write_tool_result(_task(), _hash(content), content)

    with pytest.raises(ToolResultStoreError, match="tool_result_retention_failed"):
        store.write_tool_result(_task(), _hash("overflow"), "overflow")
    assert len(list((store.run_dir(_task()) / "tool_results").glob("*.txt"))) == 100


def test_duplicate_hash_is_verified_and_not_counted_twice(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    content = "same\n"
    content_hash = _hash(content)

    first = store.write_tool_result(_task(), content_hash, content)
    second = store.write_tool_result(_task(), content_hash, content)

    assert first == second
    assert len(list(first.parent.glob("*.txt"))) == 1


def test_tool_result_read_rejects_content_drift(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    content = "original\n"
    content_hash = _hash(content)
    path = store.write_tool_result(_task(), content_hash, content)
    path.write_text("changed\n", encoding="utf-8")

    with pytest.raises(ToolResultStoreError, match="tool_result_unavailable"):
        store.read_tool_result(_task(), f"tool_result:{content_hash}")


def test_tool_result_read_rejects_hardlink_without_touching_source(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    content = "outside\n"
    content_hash = _hash(content)
    raw_dir = store.run_dir(_task()) / "tool_results"
    raw_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text(content, encoding="utf-8")
    os.link(outside, raw_dir / f"{content_hash}.txt")

    with pytest.raises(ToolResultStoreError, match="tool_result_unavailable"):
        store.read_tool_result(_task(), f"tool_result:{content_hash}")
    assert outside.read_text(encoding="utf-8") == content


def test_tool_result_read_rejects_symlink(tmp_path):
    store = RunStore(tmp_path / ".pony" / "runs")
    content = "outside\n"
    content_hash = _hash(content)
    raw_dir = store.run_dir(_task()) / "tool_results"
    raw_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text(content, encoding="utf-8")
    linked = raw_dir / f"{content_hash}.txt"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ToolResultStoreError, match="tool_result_unavailable"):
        store.read_tool_result(_task(), f"tool_result:{content_hash}")


def test_tool_result_read_rejects_root_identity_drift(tmp_path, monkeypatch):
    store = RunStore(tmp_path / ".pony" / "runs")
    content = "safe\n"
    content_hash = _hash(content)
    store.write_tool_result(_task(), content_hash, content)
    monkeypatch.setattr(
        "pony.state.run_store.private_directory_identity",
        lambda _path: ("changed", "identity"),
    )

    with pytest.raises(ToolResultStoreError, match="tool_result_unavailable"):
        store.read_tool_result(_task(), f"tool_result:{content_hash}")
