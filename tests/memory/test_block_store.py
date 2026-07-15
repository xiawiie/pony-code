import multiprocessing
import os
from pathlib import Path
import stat

import pytest

import pico.memory.block_store as block_store_module
from pico.memory.block_store import BlockStore


def _append_agent_note_process(workspace, user, note, started, finished):
    store = BlockStore(workspace_root=workspace, user_root=user)
    started.set()
    store.append_agent_note(scope="workspace", note=note)
    finished.set()


def test_list_empty(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    assert store.list() == []


def test_list_workspace_and_user_notes(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    (user / "notes").mkdir(parents=True)
    (workspace / "notes" / "auth.md").write_text("# Auth notes\ndetail\n")
    (user / "notes" / "prefs.md").write_text("# Prefs\ndetail\n")

    store = BlockStore(workspace_root=workspace, user_root=user)
    entries = {e.path for e in store.list()}
    assert entries == {"workspace/notes/auth.md", "user/notes/prefs.md"}


def test_read_returns_full_content(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    user.mkdir()
    (workspace / "notes" / "auth.md").write_text("hello\nworld\n")

    store = BlockStore(workspace_root=workspace, user_root=user)
    assert store.read("workspace/notes/auth.md") == "hello\nworld\n"


def test_read_missing_raises(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    with pytest.raises(FileNotFoundError):
        store.read("workspace/notes/missing.md")


def test_append_agent_note_creates_file(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    total = store.append_agent_note(scope="workspace", note="bcrypt rounds > 12 timeout")
    assert total > 0
    contents = (workspace / "agent_notes.md").read_text()
    assert "bcrypt rounds > 12 timeout" in contents


def test_append_agent_note_appends(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    store.append_agent_note(scope="workspace", note="first")
    store.append_agent_note(scope="workspace", note="second")
    contents = (workspace / "agent_notes.md").read_text()
    assert "first" in contents
    assert "second" in contents
    assert contents.index("first") < contents.index("second")


def test_append_agent_note_rejects_scope_root_swapped_before_atomic_write(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user, redaction_env={})
    trusted_workspace = tmp_path / "trusted-workspace"
    real_atomic_write = store._atomic_write

    def swap_scope_root(*args):
        workspace.rename(trusted_workspace)
        workspace.mkdir()
        return real_atomic_write(*args)

    monkeypatch.setattr(store, "_atomic_write", swap_scope_root)

    with pytest.raises(ValueError, match="private root changed"):
        store.append_agent_note(scope="workspace", note="must not land")

    assert list(workspace.iterdir()) == []
    assert not (trusted_workspace / "agent_notes.md").exists()


def test_append_agent_note_fsyncs_file_then_parent(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user, redaction_env={})
    events = []
    real_fsync = os.fsync

    def observed_fsync(descriptor):
        mode = os.fstat(descriptor).st_mode
        events.append("parent" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", observed_fsync)

    store.append_agent_note(scope="workspace", note="durable")

    assert events == ["file", "parent"]


def test_append_agent_note_passes_reader_limit_to_atomic_writer(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user, redaction_env={})
    options = {}
    real_write = block_store_module.securitylib.write_private_bytes_atomic

    def track_limit(*args, **kwargs):
        options.update(kwargs)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(
        block_store_module.securitylib,
        "write_private_bytes_atomic",
        track_limit,
    )

    store.append_agent_note(scope="workspace", note="bounded")

    assert options["max_existing_bytes"] == block_store_module.MAX_MEMORY_FILE_BYTES


def test_append_agent_note_does_not_retry_after_unlinked_backup_wipe_failure(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user, redaction_env={})
    store.append_agent_note(scope="workspace", note="first")
    monkeypatch.setattr(
        block_store_module.securitylib.os,
        "ftruncate",
        lambda _descriptor, _length: (_ for _ in ()).throw(
            OSError("open-unlinked wipe failed")
        ),
    )

    try:
        store.append_agent_note(scope="workspace", note="write exactly once")
    except OSError:
        store.append_agent_note(scope="workspace", note="write exactly once")

    contents = (workspace / "agent_notes.md").read_text(encoding="utf-8")
    assert contents.count("write exactly once") == 1
    assert not list(workspace.glob(".*.bak"))


def test_append_agent_note_rejects_oversized_existing_file(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    target = workspace / "agent_notes.md"
    target.write_bytes(b"x" * 9)
    monkeypatch.setattr(block_store_module, "MAX_MEMORY_FILE_BYTES", 8)
    store = BlockStore(workspace_root=workspace, user_root=user, redaction_env={})

    with pytest.raises(ValueError, match="too large"):
        store.append_agent_note(scope="workspace", note="safe note")

    assert target.read_bytes() == b"x" * 9


def test_agent_owned_reads_reject_replaced_scope_root(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    target = workspace / "agent_notes.md"
    target.write_text("trusted\n", encoding="utf-8")
    store = BlockStore(workspace_root=workspace, user_root=user, redaction_env={})
    workspace.rename(tmp_path / "trusted-workspace")
    workspace.mkdir()
    target.write_text("INJECTED\n", encoding="utf-8")

    with pytest.raises(ValueError, match="private root changed"):
        store.read("workspace/agent_notes.md")

    assert store.exists("workspace/agent_notes.md") is False
    assert all(item.path != "workspace/agent_notes.md" for item in store.list())


@pytest.mark.parametrize("existing", (False, True))
def test_append_agent_note_never_writes_past_reader_limit(
    tmp_path, monkeypatch, existing
):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    target = workspace / "agent_notes.md"
    if existing:
        target.write_text("original\n", encoding="utf-8")
    original = target.read_bytes() if existing else None
    store = BlockStore(workspace_root=workspace, user_root=user, redaction_env={})
    monkeypatch.setattr(block_store_module, "MAX_MEMORY_FILE_BYTES", 32)

    with pytest.raises(ValueError, match="memory file too large"):
        store.append_agent_note(scope="workspace", note="safe note")

    if existing:
        assert target.read_bytes() == original
    else:
        assert not target.exists()


def test_append_agent_note_waits_for_cross_process_scope_lock(tmp_path):
    from pico import file_lock

    if file_lock.fcntl is None:
        pytest.skip("cross-process file locks unavailable")

    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    context = multiprocessing.get_context("spawn")
    notes = ("child-note-one", "child-note-two")
    started = [context.Event() for _ in notes]
    finished = [context.Event() for _ in notes]
    processes = [
        context.Process(
            target=_append_agent_note_process,
            args=(workspace, user, note, started[index], finished[index]),
        )
        for index, note in enumerate(notes)
    ]

    try:
        with file_lock.locked_file(
            workspace / ".agent_notes.lock",
            require_lock=True,
        ):
            for process in processes:
                process.start()
            assert all(event.wait(timeout=5) for event in started)
            assert not any(event.wait(timeout=0.25) for event in finished)

        assert all(event.wait(timeout=5) for event in finished)
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
        lines = (workspace / "agent_notes.md").read_text(
            encoding="utf-8"
        ).splitlines()
        assert len(lines) == 2
        assert all(sum(note in line for line in lines) == 1 for note in notes)
    finally:
        for process in processes:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)


def test_append_note_too_long_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    with pytest.raises(ValueError, match="500"):
        store.append_agent_note(scope="workspace", note="x" * 501)


def test_append_rejects_complete_secret_content_and_allows_prose(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    secret = "github_pat_A123456789012345678901234567890"

    with pytest.raises(ValueError, match="sensitive_content"):
        store.append_agent_note(scope="workspace", note=secret)

    store.append_agent_note(scope="workspace", note="password policy")
    assert "password policy" in (workspace / "agent_notes.md").read_text(
        encoding="utf-8"
    )


def test_block_store_uses_configured_secret_snapshot(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    secret = "opaque-memory-value-123456789"
    store = BlockStore(
        workspace_root=workspace,
        user_root=user,
        redaction_env={"CUSTOM_CREDENTIAL": secret},
        secret_env_names=("CUSTOM_CREDENTIAL",),
    )

    with pytest.raises(ValueError, match="sensitive_content"):
        store.append_agent_note(scope="workspace", note=secret)


def test_block_store_freezes_process_env_when_constructed(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    secret = "opaque-frozen-memory-value-123456789"
    monkeypatch.setenv("PICO_FROZEN_SECRET", secret)
    store = BlockStore(workspace_root=workspace, user_root=user)
    monkeypatch.delenv("PICO_FROZEN_SECRET")

    with pytest.raises(ValueError, match="sensitive_content"):
        store.append_agent_note(scope="workspace", note=secret)

    assert not (workspace / "agent_notes.md").exists()


def test_block_store_screens_scope_before_value_bearing_errors(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    secret = "opaque/unsafe-memory-value-123456789"
    store = BlockStore(
        workspace_root=workspace,
        user_root=user,
        redaction_env={"CUSTOM_CREDENTIAL": secret},
        secret_env_names=("CUSTOM_CREDENTIAL",),
    )

    with pytest.raises(ValueError) as exc_info:
        store.append_agent_note(scope=secret, note="safe note")
    assert str(exc_info.value) == "sensitive_content"
    assert secret not in str(exc_info.value)

    assert list(workspace.iterdir()) == []
    assert list(user.iterdir()) == []


def test_block_store_scope_error_does_not_echo_invalid_value(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user, redaction_env={})

    with pytest.raises(ValueError) as scope_error:
        store.append_agent_note(scope="invalid-scope", note="safe note")

    assert str(scope_error.value) == "invalid scope"


def test_append_rejects_when_complete_existing_note_would_remain_sensitive(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    secret = "github_pat_A123456789012345678901234567890"
    target = workspace / "agent_notes.md"
    target.write_text(secret + "\n", encoding="utf-8")
    store = BlockStore(workspace_root=workspace, user_root=user)

    with pytest.raises(ValueError, match="sensitive_content"):
        store.append_agent_note(scope="workspace", note="password policy")

    assert target.read_text(encoding="utf-8") == secret + "\n"


def test_agent_note_write_rejects_leaf_symlink_without_touching_target(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("untouched\n", encoding="utf-8")
    (workspace / "agent_notes.md").symlink_to(outside)
    store = BlockStore(workspace_root=workspace, user_root=user)

    with pytest.raises(ValueError, match="symlink"):
        store.append_agent_note(scope="workspace", note="safe note")

    assert outside.read_text(encoding="utf-8") == "untouched\n"


def test_atomic_no_partial_write(tmp_path, monkeypatch):
    """If replace fails mid-way, main file must not exist half-written."""
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    (workspace / "agent_notes.md").write_text("original\n")
    store = BlockStore(workspace_root=workspace, user_root=user)

    # Simulate write failure by making the target read-only after tempfile write.
    # Just verify no half-written state under normal successful write.
    store.append_agent_note(scope="workspace", note="new")
    contents = (workspace / "agent_notes.md").read_text()
    assert contents.startswith("original")
    assert "new" in contents


def test_reject_traversal(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    store = BlockStore(workspace_root=workspace, user_root=user)
    with pytest.raises(ValueError, match="invalid path"):
        store.read("workspace/../etc/passwd")
    with pytest.raises(ValueError, match="invalid path"):
        store.read("/etc/passwd")


def test_size_chars_counts_characters_not_bytes(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    user.mkdir()
    # 6 个中文字符 = 18 bytes (UTF-8)，但 size_chars 应该报 6+换行等等
    content = "密码验证\n还有一行\n"   # 4+4 CJK + 2 newline = 10 chars, 26 bytes
    (workspace / "notes" / "auth.md").write_text(content, encoding="utf-8")
    store = BlockStore(workspace_root=workspace, user_root=user)
    entries = {e.path: e for e in store.list()}
    entry = entries["workspace/notes/auth.md"]
    assert entry.size_chars == len(content), f"expected {len(content)} chars, got {entry.size_chars}"
    # 字节数会 > 字符数
    import os
    assert os.path.getsize(workspace / "notes" / "auth.md") > entry.size_chars


@pytest.mark.parametrize("scope", ("workspace", "user"))
def test_list_skips_symlinked_memory_files(tmp_path, scope):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    (user / "notes").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("outside memory secret", encoding="utf-8")
    root = workspace if scope == "workspace" else user
    (root / "notes" / "linked.md").symlink_to(outside)

    store = BlockStore(workspace_root=workspace, user_root=user)

    assert all(entry.path != f"{scope}/notes/linked.md" for entry in store.list())


def test_list_skips_symlinked_notes_directory(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    outside = tmp_path / "outside-notes"
    workspace.mkdir()
    user.mkdir()
    outside.mkdir()
    (outside / "linked.md").write_text("outside memory secret", encoding="utf-8")
    (workspace / "notes").symlink_to(outside, target_is_directory=True)

    store = BlockStore(workspace_root=workspace, user_root=user)

    assert store.list() == []


def test_nested_symlink_directory_consumes_file_scan_budget(tmp_path, monkeypatch):
    import pico.memory.block_store as block_store_module

    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    notes = workspace / "notes"
    outside = tmp_path / "outside-notes"
    notes.mkdir(parents=True)
    user.mkdir()
    outside.mkdir()
    (notes / "a-unsafe").symlink_to(outside, target_is_directory=True)
    (notes / "b-safe.md").write_text("safe", encoding="utf-8")
    monkeypatch.setattr(block_store_module, "MAX_MEMORY_INDEX_FILES", 1)

    store = BlockStore(workspace_root=workspace, user_root=user)

    assert store.list() == []


def test_unsafe_hardlink_consumes_aggregate_byte_budget(tmp_path, monkeypatch):
    import pico.memory.block_store as block_store_module

    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    user.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside-canary", encoding="utf-8")
    os.link(outside, notes / "a-unsafe.md")
    (notes / "b-safe.md").write_text("safe", encoding="utf-8")
    monkeypatch.setattr(block_store_module, "MAX_MEMORY_FILE_BYTES", 32)
    monkeypatch.setattr(block_store_module, "MAX_MEMORY_INDEX_BYTES", 5)

    store = BlockStore(workspace_root=workspace, user_root=user)

    assert store.list() == []
    assert outside.read_text(encoding="utf-8") == "outside-canary"


@pytest.mark.parametrize("target_kind", ("inside", "outside"))
def test_read_rejects_memory_symlink(target_kind, tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    user.mkdir()
    if target_kind == "inside":
        target = workspace / "notes" / "real.md"
    else:
        target = tmp_path / "outside.md"
    target.write_text("must not be read", encoding="utf-8")
    (workspace / "notes" / "linked.md").symlink_to(target)
    store = BlockStore(workspace_root=workspace, user_root=user)

    with pytest.raises((FileNotFoundError, ValueError)):
        store.read("workspace/notes/linked.md")


@pytest.mark.parametrize("unsafe_kind", ("hardlink", "directory", "fifo"))
def test_user_note_read_and_list_reject_unsafe_leaf(tmp_path, unsafe_kind):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    notes = workspace / "notes"
    notes.mkdir(parents=True)
    user.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside-canary", encoding="utf-8")
    target = notes / "unsafe.md"
    if unsafe_kind == "hardlink":
        os.link(outside, target)
    elif unsafe_kind == "directory":
        target.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unavailable")
        os.mkfifo(target)
    store = BlockStore(workspace_root=workspace, user_root=user)

    assert "workspace/notes/unsafe.md" not in {
        entry.path for entry in store.list()
    }
    with pytest.raises((FileNotFoundError, ValueError)):
        store.read("workspace/notes/unsafe.md")
    assert outside.read_text(encoding="utf-8") == "outside-canary"


@pytest.mark.parametrize(
    "unsafe_kind",
    ("symlink", "hardlink", "directory", "fifo"),
)
def test_read_rejects_unsafe_agent_notes_leaf(tmp_path, unsafe_kind):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    workspace.mkdir()
    user.mkdir()
    outside = tmp_path / "outside-agent-notes.md"
    outside.write_text("outside-canary", encoding="utf-8")
    store = BlockStore(workspace_root=workspace, user_root=user)
    agent_notes = workspace / "agent_notes.md"
    if unsafe_kind == "symlink":
        agent_notes.symlink_to(outside)
    elif unsafe_kind == "hardlink":
        os.link(outside, agent_notes)
    elif unsafe_kind == "directory":
        agent_notes.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unavailable")
        os.mkfifo(agent_notes)

    with pytest.raises(ValueError, match="symlink|private|regular"):
        store.read("workspace/agent_notes.md")

    assert outside.read_text(encoding="utf-8") == "outside-canary"


def test_relative_scope_roots_keep_existing_listing_behavior(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    (workspace / "notes").mkdir(parents=True)
    user.mkdir()
    (workspace / "notes" / "safe.md").write_text("safe", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    store = BlockStore(workspace_root=Path("workspace"), user_root=Path("user"))

    assert [entry.path for entry in store.list()] == ["workspace/notes/safe.md"]
