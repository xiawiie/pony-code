import hashlib
from pathlib import Path
import stat
import tracemalloc

import pytest

import pico.sandbox.session as session_module
from pico.sandbox.session import SandboxSessionError, stage_source


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(session_module.STAGING_CHUNK_BYTES):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def test_stage_source_streams_128_mib_with_bounded_python_memory(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "large.bin"
    with source_file.open("wb") as handle:
        handle.truncate(session_module.MAX_FILE_BYTES)
    source_file.chmod(0o751)

    tracemalloc.start()
    try:
        result = stage_source(source, tmp_path / "staging")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    staged = tmp_path / "staging" / "large.bin"
    assert result["logical_bytes"] == session_module.MAX_FILE_BYTES
    assert result["entries"] == [
        {
            "path": "large.bin",
            "sha256": _hash_file(staged),
            "size": session_module.MAX_FILE_BYTES,
            "mode": 0o751,
            "uid": source_file.stat().st_uid,
            "gid": source_file.stat().st_gid,
        }
    ]
    assert stat.S_IMODE(staged.stat().st_mode) == 0o755
    assert peak <= 32 * 1024 * 1024


def test_stage_source_detects_known_secret_across_chunk_boundary(tmp_path):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    secret = b"cross-boundary-secret"
    (nested / "payload.bin").write_bytes(
        b"a" * (session_module.STAGING_CHUNK_BYTES - 5) + secret + b"tail"
    )

    result = stage_source(
        source,
        tmp_path / "staging",
        known_secrets=(secret,),
    )

    assert result["entries"] == []
    assert result["excluded_counts"] == {"known_secret_content": 1}
    assert list((tmp_path / "staging").iterdir()) == []


def test_stage_source_excludes_oversized_env_template(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with (source / ".env.example").open("wb") as handle:
        handle.truncate(session_module.MAX_ENV_TEMPLATE_BYTES + 1)

    result = stage_source(source, tmp_path / "staging")

    assert result["entries"] == []
    assert result["excluded_counts"] == {"env_template_too_large": 1}
    assert list((tmp_path / "staging").iterdir()) == []


def test_stage_source_detects_mode_change_during_copy_and_cleans(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "large.bin"
    source_file.write_bytes(b"x" * (session_module.STAGING_CHUNK_BYTES + 1))
    original_read = session_module.os.read
    changed = False

    def change_mode_after_read(descriptor, size):
        nonlocal changed
        chunk = original_read(descriptor, size)
        if chunk and not changed:
            changed = True
            source_file.chmod(0o600)
        return chunk

    monkeypatch.setattr(session_module.os, "read", change_mode_after_read)

    with pytest.raises(SandboxSessionError, match="workspace_changed_during_stage"):
        stage_source(source, tmp_path / "staging")

    assert not (tmp_path / "staging").exists()
    assert not list(tmp_path.rglob(".pico-stage-*"))


def test_stage_source_detects_parent_exchange_during_copy_and_cleans(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "large.bin").write_bytes(
        b"x" * (session_module.STAGING_CHUNK_BYTES + 1)
    )
    original_read = session_module.os.read
    changed = False

    def exchange_parent_after_read(descriptor, size):
        nonlocal changed
        chunk = original_read(descriptor, size)
        if chunk and not changed:
            changed = True
            nested.rename(source / "detached")
            replacement = source / "nested"
            replacement.mkdir()
            (replacement / "large.bin").write_bytes(b"replacement")
        return chunk

    monkeypatch.setattr(session_module.os, "read", exchange_parent_after_read)

    with pytest.raises(SandboxSessionError, match="workspace_changed_during_stage"):
        stage_source(source, tmp_path / "staging")

    assert not (tmp_path / "staging").exists()
    assert not list(tmp_path.rglob(".pico-stage-*"))


def test_stage_source_write_failure_removes_temp_and_destination(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "large.bin").write_bytes(
        b"x" * (session_module.STAGING_CHUNK_BYTES + 1)
    )
    original_write = session_module._write_staging_chunk
    calls = 0

    def fail_second_write(descriptor, chunk):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SandboxSessionError("staging_write_failed")
        original_write(descriptor, chunk)

    monkeypatch.setattr(session_module, "_write_staging_chunk", fail_second_write)

    with pytest.raises(SandboxSessionError, match="staging_write_failed"):
        stage_source(source, tmp_path / "staging")

    assert not (tmp_path / "staging").exists()
    assert not list(tmp_path.rglob(".pico-stage-*"))
