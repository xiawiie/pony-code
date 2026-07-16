from copy import deepcopy
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

import pico.sandbox_apply as sandbox_apply
from pico import cli as pico_cli
from pico.checkpoint_store import CheckpointStore, CheckpointStoreError
from pico.recovery_policy import DEFAULT_MAX_BLOB_SIZE
from pico.sandbox_apply import (
    _validate_capture,
    _validate_apply_journal,
    _validate_diff,
    SandboxApplyError,
    SourceApplier,
    SourceApplyStore,
    StagingObserver,
)
from pico.sandbox_session import (
    clear_source_apply_authority,
    find_project_sandbox_session,
    read_source_apply_authority,
    SandboxSessionError,
    SandboxSessionStore,
)


def _bootstrap(request):
    git = request.workspace_view.physical_root / ".git"
    git.mkdir()
    (git / "HEAD").write_text(
        "ref: refs/heads/pico-sandbox\n",
        encoding="utf-8",
    )
    return "a" * 40


def _session_metadata():
    return {
        "engine": {
            "endpoint_hash": "sha256:" + "1" * 64,
            "client_version": "29.5.2",
            "server_version": "29.5.2",
            "api_version": "1.54",
            "profile": "desktop_vm",
            "security_digest": "sha256:" + "2" * 64,
        },
        "image": {
            "reference": "sha256:" + "3" * 64,
            "manifest_digest": "sha256:" + "3" * 64,
            "image_id": "sha256:" + "4" * 64,
            "platform": "linux/arm64",
        },
        "policy": {
            "version": 1,
            "digest": "sha256:" + "5" * 64,
            "network": "none",
            "mount_digest": "sha256:" + "6" * 64,
            "resource_digest": "sha256:" + "7" * 64,
        },
    }


class _Context:
    def __init__(self, source, store, session, project_state_root=None):
        self.source_root = source
        self.execution_root = session.workspace_view.physical_root
        self.project_state_root = project_state_root or source / ".pico"
        self.sandbox_state_root = session.state_root
        self.source_apply_state_root = session.state_root
        self.sandbox_session = session
        self.runner = SimpleNamespace(session_store=store)

    def current_session(self):
        return self.runner.session_store.inspect(self.sandbox_state_root)


def _observer(
    tmp_path,
    files,
    *,
    modes=None,
    redaction_env=None,
    secret_env_names=(),
    source_profile=None,
    git_executable=None,
    project_state_root=None,
):
    source = tmp_path / "source"
    source.mkdir(parents=True)
    for name, data in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_bytes(data)
    for name, mode in (modes or {}).items():
        (source / name).chmod(mode)
    if source_profile in {"clean", "dirty", "untracked"}:
        (source / "profile.txt").write_text("clean\n", encoding="utf-8")
        for args in (
            ("init", "--quiet"),
            ("config", "user.name", "Pico Test"),
            ("config", "user.email", "pico@example.invalid"),
            ("add", "--all"),
            ("commit", "--quiet", "-m", "baseline"),
        ):
            subprocess.run(
                [git_executable, "-C", source, *args],
                check=True,
                capture_output=True,
            )
        if source_profile == "dirty":
            (source / "profile.txt").write_text("dirty\n", encoding="utf-8")
        elif source_profile == "untracked":
            (source / "untracked-profile.txt").write_text(
                "untracked\n",
                encoding="utf-8",
            )
    elif source_profile not in {None, "non_git"}:
        raise ValueError("unknown source profile")
    store = SandboxSessionStore(tmp_path / "sandboxes")
    session = store.create(
        source,
        pico_session_id="session-1",
        bootstrap_git=_bootstrap,
        git_executable=(git_executable if source_profile != "non_git" else None),
        project_state_root=project_state_root,
        **_session_metadata(),
    )
    context = _Context(source, store, session, project_state_root)
    checkpoint_store = CheckpointStore(
        session.state_root / "recovery" / ".pico" / "checkpoints"
    )
    observer = StagingObserver(
        context,
        checkpoint_store,
        redaction_env=redaction_env,
        secret_env_names=secret_env_names,
    )
    baseline = observer.ensure_baseline()
    return source, context, checkpoint_store, observer, baseline


def _modified_candidate(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    return source, context, observer, diff["diff_digest"]


def _record_rename_swaps(monkeypatch):
    calls = []
    original = sandbox_apply._rename_swap

    def record(parent, first, second, **kwargs):
        calls.append((parent, first, second, kwargs))
        return original(parent, first, second, **kwargs)

    monkeypatch.setattr(sandbox_apply, "_rename_swap", record)
    return calls


def _apply_quarantine(source, journal_id):
    return (
        source
        / ".pico"
        / "checkpoints"
        / sandbox_apply._APPLY_QUARANTINE_NAME
        / journal_id
    )


def _source_worktree_snapshot(root):
    root_info = root.lstat()
    entries = [
        (
            ".",
            root_info.st_dev,
            root_info.st_ino,
            root_info.st_mode,
            root_info.st_nlink,
            root_info.st_uid,
            root_info.st_gid,
            root_info.st_size,
            root_info.st_mtime_ns,
            root_info.st_ctime_ns,
            "",
        )
    ]
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == ".pico":
            continue
        info = path.lstat()
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        entries.append(
            (
                relative.as_posix(),
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_nlink,
                info.st_uid,
                info.st_gid,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                hashlib.sha256(data).hexdigest(),
            )
        )
    return entries


def test_final_diff_binds_baseline_blobs_and_all_ordinary_change_kinds(tmp_path):
    source, context, blobs, observer, baseline = _observer(
        tmp_path,
        {
            "modified.txt": "before\n",
            "deleted.txt": "delete me\n",
            "binary.bin": b"before\x00bytes",
            "invalid.txt": b"before\xffbytes",
        },
    )
    baseline_entries = {entry["path"]: entry for entry in baseline["entries"]}
    assert blobs.read_blob(baseline_entries["modified.txt"]["blob_ref"]) == b"before\n"
    assert observer.baseline_path.stat().st_mode & 0o777 == 0o600

    root = context.execution_root
    (root / "modified.txt").write_text("after\n", encoding="utf-8")
    (root / "deleted.txt").unlink()
    (root / "created.txt").write_text("created\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"after\x00bytes")
    (root / "invalid.txt").write_bytes(b"after\xffbytes")

    result = observer.finalize_diff(lambda text: text)
    entries = {entry["path"]: entry for entry in result["artifact"]["entries"]}

    assert result["status"] == "diff_ready"
    assert {path: entry["change_kind"] for path, entry in entries.items()} == {
        "binary.bin": "modified",
        "created.txt": "created",
        "deleted.txt": "deleted",
        "invalid.txt": "modified",
        "modified.txt": "modified",
    }
    assert "binary:modified:binary.bin" in result["artifact"]["rendered"]
    assert "binary:modified:invalid.txt" in result["artifact"]["rendered"]
    assert str(context.execution_root) not in result["artifact"]["rendered"]
    assert str(source) not in result["artifact"]["rendered"]

    artifact, final, digest = observer.load_finalized_diff(result["diff_digest"])
    current = context.current_session()
    assert artifact == result["artifact"]
    assert artifact["final_capture_digest"] == (
        "sha256:" + hashlib.sha256(observer.final_path.read_bytes()).hexdigest()
    )
    assert digest == current.manifest["diff"]["digest"]
    assert current.state == "pending_review"
    assert current.manifest["diff"] == {
        "digest": digest,
        "status": "diff_ready",
        "candidate_count": 5,
        "blocked_count": 0,
    }

    with pytest.raises(SandboxApplyError, match="sandbox_diff_not_allowed"):
        observer.finalize_diff(lambda text: text)
    (root / "created.txt").write_text("changed later\n", encoding="utf-8")
    with pytest.raises(SandboxApplyError, match="sandbox_final_tree_changed"):
        observer.load_finalized_diff(digest)


def test_final_diff_blocks_sensitive_special_and_large_entries(
    tmp_path,
    monkeypatch,
):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"README.md": "source\n"},
        redaction_env={"TOKEN": "known-secret-value"},
        secret_env_names=("TOKEN",),
    )
    root = context.execution_root
    (root / ".env").write_text("TOKEN=guest-secret\n", encoding="utf-8")
    (root / ".pico").mkdir()
    (root / ".pico" / "sessions").mkdir()
    (root / ".pico" / "sessions" / "state.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    (root / ".git" / "config").write_text("guest-only\n", encoding="utf-8")
    (root / "credentials.json").write_text("{}\n", encoding="utf-8")
    (root / "known.txt").write_text(
        "prefix known-secret-value suffix\n",
        encoding="utf-8",
    )
    (root / "large.bin").write_bytes(b"x" * (DEFAULT_MAX_BLOB_SIZE + 1))
    (root / "link").symlink_to("README.md")
    (root / "hard-a").write_text("linked\n", encoding="utf-8")
    os.link(root / "hard-a", root / "hard-b")
    os.mkfifo(root / "pipe")
    (root / ".venv").mkdir()
    (root / ".venv" / "ignored.py").write_text("ignored\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "ignored.js").write_text(
        "ignored\n",
        encoding="utf-8",
    )

    applier = SourceApplier(context, observer)
    source_before = _source_worktree_snapshot(source)
    swaps = _record_rename_swaps(monkeypatch)
    result = observer.finalize_diff(
        lambda text: text.replace("known-secret-value", "[REDACTED]")
    )
    artifact, final, _digest = observer.load_finalized_diff(result["diff_digest"])
    entries = {entry["path"]: entry for entry in artifact["entries"]}

    assert result["status"] == "diff_blocked"
    assert entries[".env"]["classification"] == "blocked_sensitive"
    assert entries[".pico"]["classification"] == "blocked_sensitive"
    assert entries["credentials.json"]["classification"] == "blocked_sensitive"
    assert entries["known.txt"]["classification"] == "blocked_sensitive"
    assert entries["large.bin"]["classification"] == "blocked_size"
    for path in ("link", "hard-a", "hard-b", "pipe"):
        assert entries[path]["classification"] == "blocked_type"
    assert ".venv/ignored.py" not in entries
    assert "node_modules/ignored.js" not in entries
    assert ".git/config" not in entries
    assert final["ignored_counts"]["ignored_generated"] >= 3
    assert "known-secret-value" not in artifact["rendered"]
    assert str(context.execution_root) not in artifact["rendered"]
    blocked_result = applier.apply(result["diff_digest"])
    assert blocked_result["status"] == "diff_blocked"
    assert swaps == []
    for path in (
        ".env",
        ".pico/sessions",
        "credentials.json",
        "known.txt",
        "large.bin",
        "link",
        "hard-a",
        "hard-b",
        "pipe",
    ):
        assert not (source / path).exists()
    assert (source / "README.md").read_text(encoding="utf-8") == "source\n"
    assert _source_worktree_snapshot(source) == source_before
    assert context.current_session().manifest["apply"] == {
        "journal_id": "",
        "status": "not_started",
    }


@pytest.mark.parametrize(
    ("path", "directory"),
    ((".pico", False), (".pico/custom.json", True), (".PICO/custom.json", True)),
)
def test_final_diff_blocks_entire_pico_namespace(tmp_path, path, directory):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"README.md": "source\n"},
    )
    candidate = context.execution_root / path
    if directory:
        candidate.parent.mkdir()
    candidate.write_text("guest state\n", encoding="utf-8")
    applier = SourceApplier(context, observer)
    source_before = _source_worktree_snapshot(source)

    diff = observer.finalize_diff(lambda text: text)
    result = applier.apply(diff["diff_digest"])

    assert diff["status"] == "diff_blocked"
    assert diff["artifact"]["counts"]["blocked_sensitive"] == 1
    assert result["status"] == "diff_blocked"
    assert _source_worktree_snapshot(source) == source_before
    assert context.current_session().manifest["apply"] == {
        "journal_id": "",
        "status": "not_started",
    }


def test_capture_and_diff_artifacts_are_exact_and_owner_only(tmp_path):
    _source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"README.md": "before\n"},
    )
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    result = observer.finalize_diff(lambda text: text)

    for path in (observer.baseline_path, observer.final_path, observer.diff_path):
        assert path.stat().st_mode & 0o777 == 0o600
    raw = json.loads(observer.diff_path.read_text(encoding="utf-8"))
    raw["unknown"] = True
    observer.diff_path.write_text(json.dumps(raw), encoding="utf-8")
    observer.diff_path.chmod(0o600)
    with pytest.raises(SandboxApplyError, match="sandbox_diff_invalid"):
        observer.load_finalized_diff(result["diff_digest"])


@pytest.mark.parametrize("crash_after", ("final", "diff"))
def test_final_diff_adopts_exact_crash_residue(tmp_path, monkeypatch, crash_after):
    _source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"README.md": "before\n"},
    )
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    original_write = sandbox_apply._write_json

    def crash_after_write(path, *args, **kwargs):
        raw = original_write(path, *args, **kwargs)
        if (crash_after == "final" and path == observer.final_path) or (
            crash_after == "diff" and path == observer.diff_path
        ):
            raise _SimulatedCrash()
        return raw

    monkeypatch.setattr(sandbox_apply, "_write_json", crash_after_write)
    with pytest.raises(_SimulatedCrash):
        observer.finalize_diff(lambda text: text)
    monkeypatch.setattr(sandbox_apply, "_write_json", original_write)

    adopted = observer.finalize_diff(lambda text: text)

    assert adopted["status"] == "diff_ready"
    assert context.current_session().state == "pending_review"
    assert observer.load_finalized_diff(adopted["diff_digest"])[0] == adopted[
        "artifact"
    ]


def test_final_diff_refuses_changed_tree_after_partial_crash(tmp_path, monkeypatch):
    _source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"README.md": "before\n"},
    )
    candidate = context.execution_root / "README.md"
    candidate.write_text("after\n", encoding="utf-8")
    original_write = sandbox_apply._write_json

    def crash_after_final(path, *args, **kwargs):
        raw = original_write(path, *args, **kwargs)
        if path == observer.final_path:
            raise _SimulatedCrash()
        return raw

    monkeypatch.setattr(sandbox_apply, "_write_json", crash_after_final)
    with pytest.raises(_SimulatedCrash):
        observer.finalize_diff(lambda text: text)
    candidate.write_text("changed later\n", encoding="utf-8")
    monkeypatch.setattr(sandbox_apply, "_write_json", original_write)

    with pytest.raises(SandboxApplyError, match="sandbox_final_tree_changed"):
        observer.finalize_diff(lambda text: text)
    assert context.current_session().state == "ready"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["counts"].pop("candidate"),
        lambda value: value.__setitem__("baseline_capture_digest", "bad"),
        lambda value: value.__setitem__("candidate_bytes", 0),
        lambda value: value["entries"][0].__setitem__("path", "../escape"),
        lambda value: value["entries"][0]["after"].__setitem__("sha256", "bad"),
        lambda value: value["entries"][0].__setitem__("change_kind", "deleted"),
    ],
)
def test_diff_validator_rejects_tampered_identity_counts_and_states(
    tmp_path,
    mutate,
):
    _source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"README.md": "before\n"},
    )
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    artifact = observer.finalize_diff(lambda text: text)["artifact"]
    tampered = deepcopy(artifact)
    mutate(tampered)

    with pytest.raises(SandboxApplyError, match="sandbox_diff_invalid"):
        _validate_diff(tampered, sandbox_id=context.sandbox_session.sandbox_id)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("tree_digest", "bad"),
        lambda value: value["entries"][0].__setitem__("path", "../escape"),
        lambda value: value["entries"][0].__setitem__("blob_ref", "0" * 64),
        lambda value: value["entries"][0].__setitem__("snapshot_eligible", False),
    ],
)
def test_capture_validator_rejects_tampered_entries(tmp_path, mutate):
    _source, context, _blobs, _observer_value, baseline = _observer(
        tmp_path,
        {"README.md": "before\n"},
    )
    tampered = deepcopy(baseline)
    mutate(tampered)

    with pytest.raises(SandboxApplyError, match="sandbox_capture_invalid"):
        _validate_capture(
            tampered,
            sandbox_id=context.sandbox_session.sandbox_id,
            capture_kind="baseline",
        )


def test_apply_validators_reject_tampered_pico_namespace(tmp_path):
    _source, context, _blobs, observer, baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    capture = deepcopy(baseline)
    capture["entries"][0]["path"] = ".pico/custom.json"
    capture["tree_digest"] = sandbox_apply._sha256(
        sandbox_apply._canonical_json(capture["entries"])
    )
    with pytest.raises(SandboxApplyError, match="sandbox_capture_invalid"):
        _validate_capture(
            capture,
            sandbox_id=context.sandbox_session.sandbox_id,
            capture_kind="baseline",
        )

    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    finalized = observer.finalize_diff(lambda text: text)
    diff = deepcopy(finalized["artifact"])
    diff["entries"][0]["path"] = ".pico/custom.json"
    with pytest.raises(SandboxApplyError, match="sandbox_diff_invalid"):
        _validate_diff(diff, sandbox_id=context.sandbox_session.sandbox_id)

    result = SourceApplier(context, observer).apply(finalized["diff_digest"])
    journal = SourceApplyStore(context.source_apply_state_root).load_journal(
        result["journal_id"]
    )
    journal["entries"][0]["path"] = ".pico/custom.json"
    journal["entries"][0]["temp_name"] = sandbox_apply._apply_temp_name(
        journal["journal_id"],
        ".pico/custom.json",
    )
    with pytest.raises(SandboxApplyError, match="sandbox_apply_journal_invalid"):
        _validate_apply_journal(journal)


def test_source_apply_creates_modifies_deletes_and_preserves_metadata(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {
            "modified.txt": "before\n",
            "deleted.txt": "delete\n",
        },
        modes={"modified.txt": 0o664},
    )
    root = context.execution_root
    (root / "modified.txt").write_text("after\n", encoding="utf-8")
    (root / "deleted.txt").unlink()
    (root / "nested").mkdir()
    (root / "nested" / "new.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "nested" / "new.sh").chmod(0o755)
    diff = observer.finalize_diff(lambda text: text)

    result = SourceApplier(context, observer).apply(diff["diff_digest"])

    assert result["status"] == "apply_applied"
    assert (source / "modified.txt").read_text(encoding="utf-8") == "after\n"
    assert (source / "modified.txt").stat().st_mode & 0o777 == 0o664
    assert not (source / "deleted.txt").exists()
    assert (source / "nested" / "new.sh").read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert (source / "nested" / "new.sh").stat().st_mode & 0o777 == 0o700
    assert (source / "nested").stat().st_mode & 0o777 == 0o700
    current = context.current_session()
    assert current.state == "applied"
    assert current.manifest["lease"] is None
    assert current.manifest["apply"]["status"] == "apply_applied"
    apply_store = SourceApplyStore(context.source_apply_state_root)
    journal = apply_store.load_journal(result["journal_id"])
    before_refs = {
        entry["before_blob_ref"]
        for entry in journal["entries"]
        if entry["before_blob_ref"]
    }
    assert before_refs
    assert all(
        not (apply_store.blobs / ref[:2] / ref).exists() for ref in before_refs
    )


def test_source_apply_rejects_custom_xattr_before_source_writes(tmp_path):
    xattr = shutil.which("xattr")
    if sys.platform != "darwin" or xattr is None:
        pytest.skip("macOS xattr command is unavailable")
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n", "b.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    (context.execution_root / "b.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    subprocess.run(
        [xattr, "-w", "user.pico_test", "value", source / "b.txt"],
        check=True,
    )
    source_before = {
        path.name: (path.lstat(), path.read_bytes())
        for path in (source / "a.txt", source / "b.txt")
    }

    with pytest.raises(
        SandboxApplyError,
        match="source_apply_metadata_unsupported",
    ):
        SourceApplier(context, observer).apply(diff["diff_digest"])

    assert {
        path.name: (path.lstat(), path.read_bytes())
        for path in (source / "a.txt", source / "b.txt")
    } == source_before
    assert context.current_session().state == "pending_review"
    assert context.current_session().manifest["apply"] == {
        "journal_id": "",
        "status": "not_started",
    }
    assert list(SourceApplyStore(context.source_apply_state_root).journals.iterdir()) == []
    assert CheckpointStore(source).source_apply_guard() is None


def test_source_apply_rejects_extended_acl_before_source_writes(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("macOS extended ACL is unavailable")
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    subprocess.run(
        ["chmod", "+a", "everyone allow read", source / "a.txt"],
        check=True,
    )
    before = ((source / "a.txt").lstat(), (source / "a.txt").read_bytes())

    with pytest.raises(
        SandboxApplyError,
        match="source_apply_metadata_unsupported",
    ):
        SourceApplier(context, observer).apply(diff["diff_digest"])

    assert ((source / "a.txt").lstat(), (source / "a.txt").read_bytes()) == before
    assert context.current_session().state == "pending_review"


def test_source_apply_rejects_inheritable_parent_acl_before_candidate_writes(
    tmp_path,
):
    if sys.platform != "darwin":
        pytest.skip("macOS extended ACL is unavailable")
    source, context, _blobs, observer, _baseline = _observer(tmp_path, {})
    (context.execution_root / "nested").mkdir()
    (context.execution_root / "nested" / "new.txt").write_text(
        "after\n",
        encoding="utf-8",
    )
    diff = observer.finalize_diff(lambda text: text)
    subprocess.run(
        [
            "chmod",
            "+a",
            "everyone allow read,file_inherit,directory_inherit",
            source,
        ],
        check=True,
    )

    with pytest.raises(
        SandboxApplyError,
        match="source_apply_metadata_unsupported",
    ):
        SourceApplier(context, observer).apply(diff["diff_digest"])

    assert not (source / "nested").exists()
    assert context.current_session().state == "pending_review"


def test_source_apply_rejects_file_flags_before_source_writes(tmp_path):
    if not hasattr(os, "chflags") or not hasattr(stat, "UF_NODUMP"):
        pytest.skip("file flags are unavailable")
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    os.chflags(source / "a.txt", stat.UF_NODUMP)
    before = ((source / "a.txt").lstat(), (source / "a.txt").read_bytes())

    with pytest.raises(
        SandboxApplyError,
        match="source_apply_metadata_unsupported",
    ):
        SourceApplier(context, observer).apply(diff["diff_digest"])

    assert ((source / "a.txt").lstat(), (source / "a.txt").read_bytes()) == before
    assert context.current_session().state == "pending_review"


def test_source_apply_external_metadata_after_prepare_requires_review(
    tmp_path,
    monkeypatch,
):
    xattr = shutil.which("xattr")
    if sys.platform != "darwin" or xattr is None:
        pytest.skip("macOS xattr command is unavailable")
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    swaps = _record_rename_swaps(monkeypatch)

    def add_metadata(stage, _path):
        if stage == "after_prepare":
            subprocess.run(
                [xattr, "-w", "user.pico_test", "value", source / "a.txt"],
                check=True,
            )

    result = SourceApplier(
        context,
        observer,
        fault_injector=add_metadata,
    ).apply(diff_digest)

    assert result["status"] == "apply_review_required"
    assert swaps == []
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert not list(source.glob(".pico-apply-*.tmp"))
    manifest = json.loads(
        (context.sandbox_state_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["state"] == "review_required"
    assert CheckpointStore(source).source_apply_guard() is not None


def test_source_apply_delete_preserves_external_edit_before_commit(
    tmp_path,
    monkeypatch,
):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"delete.txt": "before\n"},
    )
    (context.execution_root / "delete.txt").unlink()
    diff = observer.finalize_diff(lambda text: text)
    renames = []
    original = sandbox_apply._rename_noreplace

    def record(*args, **kwargs):
        renames.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(sandbox_apply, "_rename_noreplace", record)

    def external_edit(stage, _path):
        if stage == "before_delete_commit":
            (source / "delete.txt").write_text("external\n", encoding="utf-8")

    result = SourceApplier(
        context,
        observer,
        fault_injector=external_edit,
    ).apply(diff["diff_digest"])

    assert result["status"] == "apply_review_required"
    assert renames == []
    assert (source / "delete.txt").read_text(encoding="utf-8") == "external\n"
    assert not list(source.glob(".pico-apply-*.tmp"))


def test_source_apply_delete_crash_keeps_journal_bound_tombstone(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"delete.txt": "before\n"},
    )
    (context.execution_root / "delete.txt").unlink()
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_delete(stage, _path):
        if stage == "after_replace":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_delete,
        ).apply(diff["diff_digest"])

    journal_id = context.current_session().manifest["apply"]["journal_id"]
    journal = SourceApplyStore(context.source_apply_state_root).load_journal(journal_id)
    tombstone = _apply_quarantine(source, journal_id) / journal["entries"][0][
        "temp_name"
    ]
    assert not (source / "delete.txt").exists()
    assert not list(source.glob(".pico-apply-*.tmp"))
    assert tombstone.read_text(encoding="utf-8") == "before\n"

    result = SourceApplier(context, observer).reconcile()

    assert result["status"] == "apply_applied"
    assert not (source / "delete.txt").exists()
    assert not tombstone.exists()


def test_source_apply_delete_missing_active_tombstone_requires_review(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"delete.txt": "before\n"},
    )
    (context.execution_root / "delete.txt").unlink()
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_delete(stage, _path):
        if stage == "after_replace":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_delete,
        ).apply(diff["diff_digest"])

    journal_id = context.current_session().manifest["apply"]["journal_id"]
    journal = SourceApplyStore(context.source_apply_state_root).load_journal(journal_id)
    tombstone = _apply_quarantine(source, journal_id) / journal["entries"][0][
        "temp_name"
    ]
    tombstone.unlink()

    result = SourceApplier(context, observer).reconcile()

    assert result["status"] == "apply_review_required"
    assert not (source / "delete.txt").exists()
    manifest = json.loads(
        (context.sandbox_state_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["state"] == "review_required"
    assert CheckpointStore(source).source_apply_guard() is not None


def test_source_apply_rejects_same_state_inode_replacement_after_journal(tmp_path):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    replacement_identity = None

    def replace_with_same_state(stage, _path):
        nonlocal replacement_identity
        if stage != "after_journal":
            return
        target = source / "a.txt"
        mode = stat.S_IMODE(target.stat().st_mode)
        replacement = source / "replacement.txt"
        replacement.write_bytes(target.read_bytes())
        replacement.chmod(mode)
        os.replace(replacement, target)
        info = target.stat()
        replacement_identity = (info.st_dev, info.st_ino)

    result = SourceApplier(
        context,
        observer,
        fault_injector=replace_with_same_state,
    ).apply(diff_digest)

    current = (source / "a.txt").stat()
    assert result["status"] == "apply_review_required"
    assert (current.st_dev, current.st_ino) == replacement_identity
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert not list(source.glob(".pico-apply-*.tmp"))
    assert CheckpointStore(source).source_apply_guard() is not None


def test_source_apply_delete_quarantine_collision_does_not_overwrite(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"delete.txt": "before\n"},
    )
    (context.execution_root / "delete.txt").unlink()
    diff = observer.finalize_diff(lambda text: text)
    collision = None

    def create_collision(stage, _path):
        nonlocal collision
        if stage != "after_journal":
            return
        journal_id = context.current_session().manifest["apply"]["journal_id"]
        journal = SourceApplyStore(context.source_apply_state_root).load_journal(
            journal_id
        )
        collision = _apply_quarantine(source, journal_id) / journal["entries"][0][
            "temp_name"
        ]
        collision.write_text("external\n", encoding="utf-8")
        collision.chmod(0o600)

    result = SourceApplier(
        context,
        observer,
        fault_injector=create_collision,
    ).apply(diff["diff_digest"])

    assert result["status"] == "apply_review_required"
    assert (source / "delete.txt").read_text(encoding="utf-8") == "before\n"
    assert collision is not None
    assert collision.read_text(encoding="utf-8") == "external\n"
    assert CheckpointStore(source).source_apply_guard() is not None


def test_source_apply_quarantine_mode_change_requires_review(tmp_path):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    quarantine = None

    def widen_quarantine(stage, _path):
        nonlocal quarantine
        if stage != "after_journal":
            return
        journal_id = context.current_session().manifest["apply"]["journal_id"]
        quarantine = _apply_quarantine(source, journal_id)
        quarantine.chmod(0o755)

    result = SourceApplier(
        context,
        observer,
        fault_injector=widen_quarantine,
    ).apply(diff_digest)

    assert result["status"] == "apply_review_required"
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert quarantine is not None
    assert stat.S_IMODE(quarantine.stat().st_mode) == 0o755
    assert CheckpointStore(source).source_apply_guard() is not None


def test_source_apply_quarantine_replacement_before_attach_requires_review(
    tmp_path,
    monkeypatch,
):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    original = sandbox_apply._ensure_apply_quarantine

    def replace_after_ensure(store, journal_id):
        path, identity = original(store, journal_id)
        path.rename(path.with_name(path.name + "-detached"))
        path.mkdir(mode=0o700)
        path.chmod(0o700)
        return path, identity

    monkeypatch.setattr(
        sandbox_apply,
        "_ensure_apply_quarantine",
        replace_after_ensure,
    )

    result = SourceApplier(context, observer).apply(diff_digest)

    assert result["status"] == "apply_review_required"
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert CheckpointStore(source).source_apply_guard() is not None


def test_source_apply_delete_noreplace_unsupported_is_zero_write(
    tmp_path,
    monkeypatch,
):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"delete.txt": "before\n"},
    )
    before = (source / "delete.txt").stat()
    (context.execution_root / "delete.txt").unlink()
    diff = observer.finalize_diff(lambda text: text)
    monkeypatch.setattr(
        sandbox_apply,
        "_rename_noreplace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.ENOTSUP, "unsupported")
        ),
    )

    result = SourceApplier(context, observer).apply(diff["diff_digest"])

    after = (source / "delete.txt").stat()
    assert result["status"] == "apply_failed_rolled_back"
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert (source / "delete.txt").read_text(encoding="utf-8") == "before\n"
    assert CheckpointStore(source).source_apply_guard() is None


def test_source_apply_created_file_rollback_cleans_private_temp(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(tmp_path, {})
    (context.execution_root / "created.txt").write_text(
        "created\n",
        encoding="utf-8",
    )
    diff = observer.finalize_diff(lambda text: text)

    def fail_after_mutation(stage, _path):
        if stage == "after_mutation":
            raise OSError("injected")

    result = SourceApplier(
        context,
        observer,
        fault_injector=fail_after_mutation,
    ).apply(diff["diff_digest"])
    journal = SourceApplyStore(context.source_apply_state_root).load_journal(
        result["journal_id"]
    )

    assert result["status"] == "apply_failed_rolled_back"
    assert not (source / "created.txt").exists()
    assert all(journal["entries"][0]["prepared_identity"].values())
    assert not _apply_quarantine(source, result["journal_id"]).exists()
    assert CheckpointStore(source).source_apply_guard() is None


def test_source_apply_reconciles_crash_after_rollback_prepare(tmp_path):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)

    def crash_during_rollback(stage, _path):
        if stage == "after_mutation":
            raise OSError("start rollback")
        if stage == "rollback_after_prepare":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_during_rollback,
        ).apply(diff_digest)

    journal_id = context.current_session().manifest["apply"]["journal_id"]
    journal = SourceApplyStore(context.source_apply_state_root).load_journal(journal_id)
    entry = journal["entries"][0]
    current = (source / "a.txt").stat()
    assert (current.st_dev, current.st_ino) == (
        entry["after_identity"]["device"],
        entry["after_identity"]["inode"],
    )
    assert entry["prepared_identity"] != entry["after_identity"]

    result = SourceApplier(context, observer).reconcile()

    assert result["status"] == "apply_failed_rolled_back"
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert not _apply_quarantine(source, journal_id).exists()
    assert CheckpointStore(source).source_apply_guard() is None


def test_source_apply_delete_tombstone_counts_against_cleanup_budget(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"delete.txt": "before\n"},
    )
    (context.execution_root / "delete.txt").unlink()
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_terminalize(stage, _path):
        if stage == "after_terminalize":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_terminalize,
        ).apply(diff["diff_digest"])

    journal_id = context.current_session().manifest["apply"]["journal_id"]
    journal = SourceApplyStore(context.source_apply_state_root).load_journal(journal_id)
    tombstone = _apply_quarantine(source, journal_id) / journal["entries"][0][
        "temp_name"
    ]
    cleanup = SourceApplyStore(context.source_apply_state_root).cleanup_terminal_blobs(
        journal_id,
        max_entries=0,
    )

    assert cleanup["complete"] is False
    assert cleanup["removed_count"] == 0
    assert tombstone.read_text(encoding="utf-8") == "before\n"
    assert CheckpointStore(source).source_apply_guard() is not None

    cleanup = SourceApplyStore(context.source_apply_state_root).cleanup_terminal_blobs(
        journal_id
    )
    assert cleanup["complete"] is True
    mutation_store = CheckpointStore(source)
    with mutation_store.mutation_lock(source_apply_journal_id=journal_id):
        mutation_store.finish_source_apply_guard(journal_id=journal_id)


def test_terminal_delete_cleanup_preserves_recreated_target(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"delete.txt": "before\n"},
    )
    (context.execution_root / "delete.txt").unlink()
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_terminalize(stage, _path):
        if stage == "after_terminalize":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_terminalize,
        ).apply(diff["diff_digest"])

    journal_id = context.current_session().manifest["apply"]["journal_id"]
    recreated = source / "delete.txt"
    recreated.write_text("external\n", encoding="utf-8")
    identity = (recreated.stat().st_dev, recreated.stat().st_ino)

    cleanup = SourceApplyStore(context.source_apply_state_root).cleanup_terminal_blobs(
        journal_id
    )

    assert cleanup["complete"] is True
    assert recreated.read_text(encoding="utf-8") == "external\n"
    assert (recreated.stat().st_dev, recreated.stat().st_ino) == identity
    mutation_store = CheckpointStore(source)
    with mutation_store.mutation_lock(source_apply_journal_id=journal_id):
        mutation_store.finish_source_apply_guard(journal_id=journal_id)


def test_terminal_delete_cleanup_ignores_replaced_source_parent(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"nested/delete.txt": "before\n"},
    )
    (context.execution_root / "nested" / "delete.txt").unlink()
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_terminalize(stage, _path):
        if stage == "after_terminalize":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_terminalize,
        ).apply(diff["diff_digest"])

    journal_id = context.current_session().manifest["apply"]["journal_id"]
    detached = source / "nested-detached"
    (source / "nested").rename(detached)
    recreated_parent = source / "nested"
    recreated_parent.mkdir()
    recreated = recreated_parent / "delete.txt"
    recreated.write_text("external\n", encoding="utf-8")
    parent_identity = (
        recreated_parent.stat().st_dev,
        recreated_parent.stat().st_ino,
    )

    cleanup = SourceApplyStore(context.source_apply_state_root).cleanup_terminal_blobs(
        journal_id
    )

    assert cleanup["complete"] is True
    assert recreated.read_text(encoding="utf-8") == "external\n"
    assert (recreated_parent.stat().st_dev, recreated_parent.stat().st_ino) == (
        parent_identity
    )
    assert detached.is_dir()
    mutation_store = CheckpointStore(source)
    with mutation_store.mutation_lock(source_apply_journal_id=journal_id):
        mutation_store.finish_source_apply_guard(journal_id=journal_id)


def test_rolled_back_cleanup_failure_is_observable_and_keeps_guard(
    tmp_path,
    monkeypatch,
):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    monkeypatch.setattr(
        SourceApplyStore,
        "cleanup_terminal_blobs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SandboxApplyError("sandbox_apply_cleanup_failed")
        ),
    )

    def fail_after_mutation(stage, _path):
        if stage == "after_mutation":
            raise OSError("injected")

    result = SourceApplier(
        context,
        observer,
        fault_injector=fail_after_mutation,
    ).apply(diff_digest)

    assert result["status"] == "apply_failed_rolled_back"
    assert result["cleanup_status"] == "pending"
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert CheckpointStore(source).source_apply_guard() is not None


def test_linux_metadata_probe_failure_is_fail_closed(monkeypatch):
    info = SimpleNamespace(st_flags=0, st_mode=stat.S_IFREG | 0o600)
    monkeypatch.setattr(sandbox_apply.sys, "platform", "linux")
    monkeypatch.setattr(
        sandbox_apply.fcntl,
        "ioctl",
        lambda *_args: (_ for _ in ()).throw(OSError(errno.ENOTTY, "unsupported")),
    )

    with pytest.raises(
        SandboxApplyError,
        match="source_apply_metadata_unsupported",
    ):
        sandbox_apply._descriptor_file_flags(0, info)


def test_source_apply_accepts_platform_default_metadata(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    result = SourceApplier(context, observer).apply(diff["diff_digest"])

    assert result["status"] == "apply_applied"
    assert (source / "a.txt").read_text(encoding="utf-8") == "after\n"


@pytest.mark.parametrize("source_profile", ["clean", "dirty", "untracked", "non_git"])
def test_source_apply_contract_is_independent_of_source_profile(
    tmp_path,
    source_profile,
):
    git_executable = (
        "/usr/bin/git" if Path("/usr/bin/git").is_file() else shutil.which("git")
    )
    if source_profile != "non_git" and git_executable is None:
        pytest.skip("git executable is required for Git source profiles")
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"modified.txt": "before\n", "deleted.txt": "delete\n"},
        source_profile=source_profile,
        git_executable=git_executable,
    )
    staging_baseline = json.loads(
        (context.sandbox_state_root / "baseline.json").read_text(encoding="utf-8")
    )
    profile_paths = ("profile.txt", "untracked-profile.txt")

    def profile_status():
        if source_profile == "non_git":
            return b""
        return subprocess.run(
            [
                git_executable,
                "-C",
                source,
                "status",
                "--porcelain=v1",
                "-z",
                "--",
                *profile_paths,
            ],
            check=True,
            capture_output=True,
        ).stdout

    status_before = profile_status()
    if source_profile == "non_git":
        assert not (source / ".git").exists()
        assert staging_baseline["tracked_paths"] == []
        assert staging_baseline["untracked_paths"] == ["deleted.txt", "modified.txt"]
        assert context.current_session().manifest["source"]["branch"] == ""
        assert context.current_session().manifest["source"]["head"] == ""
    else:
        assert set(staging_baseline["tracked_paths"]) == {
            "deleted.txt",
            "modified.txt",
            "profile.txt",
        }
        assert staging_baseline["untracked_paths"] == (
            ["untracked-profile.txt"] if source_profile == "untracked" else []
        )
        assert status_before == {
            "clean": b"",
            "dirty": b" M profile.txt\0",
            "untracked": b"?? untracked-profile.txt\0",
        }[source_profile]
        assert context.current_session().manifest["source"]["branch"]
        assert context.current_session().manifest["source"]["head"]
    (context.execution_root / "modified.txt").write_text(
        "after\n",
        encoding="utf-8",
    )
    (context.execution_root / "deleted.txt").unlink()
    (context.execution_root / "created.txt").write_text(
        "created\n",
        encoding="utf-8",
    )
    diff = observer.finalize_diff(lambda text: text)

    result = SourceApplier(context, observer).apply(diff["diff_digest"])

    assert result["status"] == "apply_applied"
    assert (source / "modified.txt").read_text(encoding="utf-8") == "after\n"
    assert not (source / "deleted.txt").exists()
    assert (source / "created.txt").read_text(encoding="utf-8") == "created\n"
    assert profile_status() == status_before
    current = context.current_session()
    assert current.state == "applied"
    assert current.manifest["apply"]["status"] == "apply_applied"


def test_source_apply_conflict_and_blocked_diff_write_no_candidate_files(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"README.md": "before\n"},
    )
    (context.execution_root / "README.md").write_text("candidate\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    (source / "README.md").write_text("external\n", encoding="utf-8")

    result = SourceApplier(context, observer).apply(diff["diff_digest"])

    assert result["status"] == "apply_conflicted"
    assert (source / "README.md").read_text(encoding="utf-8") == "external\n"
    assert context.current_session().manifest["apply"] == {
        "journal_id": "",
        "status": "apply_conflicted",
    }

    source2, context2, _blobs2, observer2, _baseline2 = _observer(
        tmp_path / "blocked",
        {"README.md": "before\n"},
    )
    (context2.execution_root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    blocked = observer2.finalize_diff(lambda text: text)
    blocked_result = SourceApplier(context2, observer2).apply(
        blocked["diff_digest"]
    )

    assert blocked_result["status"] == "diff_blocked"
    assert not (source2 / ".env").exists()
    assert context2.current_session().manifest["apply"] == {
        "journal_id": "",
        "status": "not_started",
    }


def test_source_apply_staging_change_and_symlink_parent_are_conflicts(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"README.md": "before\n"},
    )
    (context.execution_root / "README.md").write_text("candidate\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    (context.execution_root / "README.md").write_text("changed later\n", encoding="utf-8")

    result = SourceApplier(context, observer).apply(diff["diff_digest"])

    assert result["status"] == "apply_conflicted"
    assert (source / "README.md").read_text(encoding="utf-8") == "before\n"

    source2, context2, _blobs2, observer2, _baseline2 = _observer(
        tmp_path / "symlink",
        {"README.md": "before\n"},
    )
    (context2.execution_root / "nested").mkdir()
    (context2.execution_root / "nested" / "new.txt").write_text(
        "candidate\n",
        encoding="utf-8",
    )
    diff2 = observer2.finalize_diff(lambda text: text)
    outside = tmp_path / "outside"
    outside.mkdir()
    (source2 / "nested").symlink_to(outside, target_is_directory=True)

    result2 = SourceApplier(context2, observer2).apply(diff2["diff_digest"])

    assert result2["status"] == "apply_conflicted"
    assert not (outside / "new.txt").exists()


def test_source_apply_fault_rolls_back_all_candidate_files(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before-a\n", "b.txt": "before-b\n"},
    )
    (context.execution_root / "a.txt").write_text("after-a\n", encoding="utf-8")
    (context.execution_root / "b.txt").write_text("after-b\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def fail_after_first(stage, path):
        if stage == "after_mutation" and path == "a.txt":
            raise OSError("injected")

    result = SourceApplier(
        context,
        observer,
        fault_injector=fail_after_first,
    ).apply(diff["diff_digest"])

    assert result["status"] == "apply_failed_rolled_back"
    assert (source / "a.txt").read_text(encoding="utf-8") == "before-a\n"
    assert (source / "b.txt").read_text(encoding="utf-8") == "before-b\n"
    current = context.current_session()
    assert current.state == "pending_review"
    assert current.manifest["apply"]["status"] == "apply_failed_rolled_back"


def test_source_apply_unknown_rollback_state_requires_review(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def make_rollback_uncertain(stage, path):
        if stage == "after_mutation" and path == "a.txt":
            raise OSError("injected")
        if stage == "before_rollback":
            (source / "a.txt").write_text("external\n", encoding="utf-8")

    result = SourceApplier(
        context,
        observer,
        fault_injector=make_rollback_uncertain,
    ).apply(diff["diff_digest"])

    assert result["status"] == "apply_review_required"
    assert (source / "a.txt").read_text(encoding="utf-8") == "external\n"
    current = context.current_session()
    assert current.state == "review_required"
    assert current.manifest["apply"]["status"] == "apply_review_required"


def test_source_apply_detects_external_edit_after_lock_without_write(
    tmp_path,
    monkeypatch,
):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    swaps = _record_rename_swaps(monkeypatch)

    def edit_after_lock(stage, _path):
        if stage == "after_lock":
            (source / "a.txt").write_text("external\n", encoding="utf-8")

    result = SourceApplier(
        context,
        observer,
        fault_injector=edit_after_lock,
    ).apply(diff_digest)

    assert result["status"] == "apply_conflicted"
    assert swaps == []
    assert (source / "a.txt").read_text(encoding="utf-8") == "external\n"
    current = context.current_session()
    assert current.state == "pending_review"
    assert current.manifest["apply"]["status"] == "apply_conflicted"


def test_source_apply_external_edit_after_prepare_requires_review_without_write(
    tmp_path,
    monkeypatch,
):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    swaps = _record_rename_swaps(monkeypatch)

    def edit_after_prepare(stage, _path):
        if stage == "after_prepare":
            (source / "a.txt").write_text("external\n", encoding="utf-8")

    result = SourceApplier(
        context,
        observer,
        fault_injector=edit_after_prepare,
    ).apply(diff_digest)

    assert result["status"] == "apply_review_required"
    assert swaps == []
    assert (source / "a.txt").read_text(encoding="utf-8") == "external\n"
    assert not list(source.glob(".pico-apply-*.tmp"))
    current = context.current_session()
    assert current.state == "review_required"
    assert current.manifest["apply"]["status"] == "apply_review_required"
    with pytest.raises(SandboxApplyError, match="sandbox_apply_not_allowed"):
        SourceApplier(context, observer).apply(diff_digest)


@pytest.mark.parametrize(
    ("stage", "expected_swaps"),
    [
        ("after_prepare", 0),
        ("after_replace", 2),
        ("after_parent_fsync", 2),
    ],
)
def test_source_apply_fault_stage_rolls_back_exactly(
    tmp_path,
    monkeypatch,
    stage,
    expected_swaps,
):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    swaps = _record_rename_swaps(monkeypatch)

    def fail_at_stage(current, _path):
        if current == stage:
            raise OSError("injected")

    result = SourceApplier(
        context,
        observer,
        fault_injector=fail_at_stage,
    ).apply(diff_digest)

    assert result["status"] == "apply_failed_rolled_back"
    assert len(swaps) == expected_swaps
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert not list(source.glob(".pico-apply-*.tmp"))
    current = context.current_session()
    assert current.state == "pending_review"
    assert current.manifest["apply"]["status"] == "apply_failed_rolled_back"


@pytest.mark.parametrize(
    ("fault", "error_number"),
    [
        ("disk_full", errno.ENOSPC),
        ("permission", errno.EACCES),
        ("readonly", errno.EROFS),
    ],
)
def test_source_apply_io_faults_roll_back_without_temp_residue(
    tmp_path,
    monkeypatch,
    fault,
    error_number,
):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    source_identity = (source.stat().st_dev, source.stat().st_ino)
    original_open = os.open
    original_write = os.write
    original_fsync = os.fsync
    apply_descriptors = set()
    injected = False

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal injected
        is_apply_temp = (
            isinstance(path, str)
            and path.startswith(".pico-apply-")
            and flags & os.O_WRONLY
            and dir_fd is not None
        )
        if fault == "permission" and is_apply_temp and not injected:
            injected = True
            raise OSError(error_number, os.strerror(error_number))
        if dir_fd is None:
            descriptor = original_open(path, flags, mode)
        else:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if is_apply_temp:
            apply_descriptors.add(descriptor)
        return descriptor

    def faulting_write(descriptor, data):
        nonlocal injected
        if fault == "disk_full" and descriptor in apply_descriptors and not injected:
            injected = True
            raise OSError(error_number, os.strerror(error_number))
        return original_write(descriptor, data)

    def faulting_fsync(descriptor):
        nonlocal injected
        info = os.fstat(descriptor)
        if (
            fault == "readonly"
            and (info.st_dev, info.st_ino) == source_identity
            and not injected
        ):
            injected = True
            raise OSError(error_number, os.strerror(error_number))
        return original_fsync(descriptor)

    monkeypatch.setattr(sandbox_apply.os, "open", tracked_open)
    monkeypatch.setattr(
        sandbox_apply.os,
        "supports_dir_fd",
        {*os.supports_dir_fd, tracked_open},
    )
    monkeypatch.setattr(sandbox_apply.os, "write", faulting_write)
    monkeypatch.setattr(sandbox_apply.os, "fsync", faulting_fsync)

    result = SourceApplier(context, observer).apply(diff_digest)

    assert injected is True
    assert result["status"] == "apply_failed_rolled_back"
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert not list(source.glob(".pico-apply-*.tmp"))
    current = context.current_session()
    assert current.state == "pending_review"
    assert current.manifest["apply"]["status"] == "apply_failed_rolled_back"


def test_source_apply_rollback_fault_requires_review_with_provable_before(
    tmp_path,
    monkeypatch,
):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    swaps = _record_rename_swaps(monkeypatch)

    def fail_during_rollback(stage, _path):
        if stage in {"after_mutation", "rollback_after_replace"}:
            raise OSError("injected")

    result = SourceApplier(
        context,
        observer,
        fault_injector=fail_during_rollback,
    ).apply(diff_digest)

    assert result["status"] == "apply_review_required"
    assert len(swaps) == 2
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"
    current = context.current_session()
    assert current.state == "review_required"
    assert current.manifest["apply"]["status"] == "apply_review_required"
    with pytest.raises(SandboxApplyError, match="sandbox_apply_not_allowed"):
        SourceApplier(context, observer).apply(diff_digest)


class _SimulatedCrash(BaseException):
    pass


def test_source_apply_adopts_journal_left_before_session_begin(tmp_path, monkeypatch):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    session_store = context.runner.session_store
    original_begin_apply = session_store.begin_apply

    def crash_before_session_begin(*_args, **_kwargs):
        raise _SimulatedCrash()

    monkeypatch.setattr(session_store, "begin_apply", crash_before_session_begin)
    with pytest.raises(_SimulatedCrash):
        SourceApplier(context, observer).apply(diff_digest)

    current = context.current_session()
    assert current.state == "pending_review"
    assert current.manifest["apply"] == {"journal_id": "", "status": "not_started"}
    apply_store = SourceApplyStore(context.source_apply_state_root)
    journal_paths = list(apply_store.journals.glob("apply_*.json"))
    assert len(journal_paths) == 1
    orphan = apply_store.load_journal(
        journal_paths[0].stem,
        sandbox_id=current.sandbox_id,
    )
    assert orphan["status"] == "applying"
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"

    monkeypatch.setattr(session_store, "begin_apply", original_begin_apply)
    result = SourceApplier(context, observer).apply(diff_digest)

    assert result["status"] == "apply_failed_rolled_back"
    assert result["journal_id"] == orphan["journal_id"]
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert len(list(apply_store.journals.glob("apply_*.json"))) == 1
    current = context.current_session()
    assert current.state == "pending_review"
    assert current.manifest["apply"] == {
        "journal_id": orphan["journal_id"],
        "status": "apply_failed_rolled_back",
    }


def test_source_apply_crash_after_journal_before_session_is_exactly_retriable(
    tmp_path,
):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)

    def crash_after_journal(stage, _path):
        if stage == "after_journal_before_guard":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_journal,
        ).apply(diff_digest)

    authority = read_source_apply_authority(
        context.runner.session_store.parent,
        source,
    )
    journal = SourceApplyStore(context.source_apply_state_root).load_journal(
        authority["journal_id"],
        sandbox_id=authority["sandbox_id"],
    )
    assert journal["status"] == "applying"
    assert context.current_session().state == "pending_review"

    result = SourceApplier(context, observer).apply(diff_digest)

    assert result["status"] == "apply_failed_rolled_back"
    assert result["journal_id"] == authority["journal_id"]
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"


def test_source_apply_rejects_ambiguous_unclaimed_journals(tmp_path, monkeypatch):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    session_store = context.runner.session_store
    original_begin_apply = session_store.begin_apply
    monkeypatch.setattr(
        session_store,
        "begin_apply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_SimulatedCrash()),
    )
    with pytest.raises(_SimulatedCrash):
        SourceApplier(context, observer).apply(diff_digest)

    apply_store = SourceApplyStore(context.source_apply_state_root)
    first_path = next(apply_store.journals.glob("apply_*.json"))
    duplicate = json.loads(first_path.read_text(encoding="utf-8"))
    duplicate["journal_id"] = "apply_" + "f" * 32
    for entry in duplicate["entries"]:
        entry["temp_name"] = sandbox_apply._apply_temp_name(
            duplicate["journal_id"],
            entry["path"],
        )
    apply_store.write_journal(duplicate)
    monkeypatch.setattr(session_store, "begin_apply", original_begin_apply)

    with pytest.raises(SandboxApplyError, match="sandbox_apply_journal_invalid"):
        SourceApplier(context, observer).apply(diff_digest)

    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert context.current_session().manifest["apply"] == {
        "journal_id": "",
        "status": "not_started",
    }


def test_source_apply_reconciles_crash_before_terminalize(tmp_path, monkeypatch):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    swaps = _record_rename_swaps(monkeypatch)

    def crash_before_terminalize(stage, _path):
        if stage == "before_terminalize":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_before_terminalize,
        ).apply(diff_digest)

    assert len(swaps) == 1
    assert (source / "a.txt").read_text(encoding="utf-8") == "after\n"
    current = context.current_session()
    assert current.state == "applying"
    assert current.manifest["apply"]["status"] == "applying"

    result = SourceApplier(context, observer).reconcile()

    assert result["status"] == "apply_applied"


def test_unresolved_source_apply_blocks_other_pico_mutations(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path, {"a.txt": "before\n"}
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def crash_before_terminalize(stage, _path):
        if stage == "before_terminalize":
            raise KeyboardInterrupt("crash")

    with pytest.raises(KeyboardInterrupt, match="crash"):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_before_terminalize,
        ).apply(diff["diff_digest"])

    source_store = CheckpointStore(source)
    with pytest.raises(CheckpointStoreError, match="source_apply_review_required"):
        with source_store.mutation_lock():
            pass
    assert (source / "a.txt").read_text(encoding="utf-8") == "after\n"

    result = SourceApplier(context, observer).reconcile()
    assert result["status"] == "apply_applied"
    with source_store.mutation_lock():
        pass
    assert context.current_session().state == "applied"


def test_source_apply_crash_after_terminalize_is_already_terminal(
    tmp_path,
    monkeypatch,
):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    swaps = _record_rename_swaps(monkeypatch)

    def crash_after_terminalize(stage, _path):
        if stage == "after_terminalize":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_terminalize,
        ).apply(diff_digest)

    assert len(swaps) == 1
    assert (source / "a.txt").read_text(encoding="utf-8") == "after\n"
    current = context.current_session()
    assert current.state == "applied"
    assert current.manifest["apply"]["status"] == "apply_applied"
    assert context.execution_root.is_dir()
    with pytest.raises(SandboxApplyError, match="sandbox_apply_not_reconcilable"):
        SourceApplier(context, observer).reconcile()


def test_source_apply_reconciles_crash_after_all_source_bytes_committed(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_commit(stage, path):
        if stage == "after_mutation" and path == "a.txt":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_commit,
        ).apply(diff["diff_digest"])

    assert context.current_session().state == "applying"
    result = SourceApplier(context, observer).reconcile()

    assert result["status"] == "apply_applied"
    assert (source / "a.txt").read_text(encoding="utf-8") == "after\n"
    assert context.current_session().state == "applied"


def test_source_apply_reconciles_mixed_crash_state_by_rolling_back(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before-a\n", "b.txt": "before-b\n"},
    )
    (context.execution_root / "a.txt").write_text("after-a\n", encoding="utf-8")
    (context.execution_root / "b.txt").write_text("after-b\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_first(stage, path):
        if stage == "after_mutation" and path == "a.txt":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_first,
        ).apply(diff["diff_digest"])

    result = SourceApplier(context, observer).reconcile()

    assert result["status"] == "apply_failed_rolled_back"
    assert (source / "a.txt").read_text(encoding="utf-8") == "before-a\n"
    assert (source / "b.txt").read_text(encoding="utf-8") == "before-b\n"
    assert context.current_session().state == "pending_review"


def test_source_apply_reconciles_crash_before_first_source_write(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_journal(stage, _path):
        if stage == "after_journal":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_journal,
        ).apply(diff["diff_digest"])

    result = SourceApplier(context, observer).reconcile()

    assert result["status"] == "apply_failed_rolled_back"
    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"


@pytest.mark.parametrize("created", [False, True])
def test_source_apply_reconciles_crash_between_replace_and_temp_cleanup(
    tmp_path,
    created,
):
    files = {} if created else {"a.txt": "before\n"}
    source, context, _blobs, observer, _baseline = _observer(tmp_path, files)
    target = context.execution_root / ("new.txt" if created else "a.txt")
    target.write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_replace(stage, _path):
        if stage == "after_replace":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_replace,
        ).apply(diff["diff_digest"])

    source_target = source / target.name
    assert source_target.read_text(encoding="utf-8") == "after\n"
    assert source_target.stat().st_nlink == 1
    result = SourceApplier(context, observer).reconcile()

    assert result["status"] == "apply_applied"
    assert source_target.read_text(encoding="utf-8") == "after\n"
    assert source_target.stat().st_nlink == 1
    assert not list(source.glob(".pico-apply-*.tmp"))


def test_source_apply_reconciles_crash_after_created_directory(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(tmp_path, {})
    (context.execution_root / "nested").mkdir()
    (context.execution_root / "nested" / "new.txt").write_text(
        "after\n",
        encoding="utf-8",
    )
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_mkdir(stage, _path):
        if stage == "after_mkdir":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_mkdir,
        ).apply(diff["diff_digest"])

    assert (source / "nested").is_dir()
    result = SourceApplier(context, observer).reconcile()

    assert result["status"] == "apply_failed_rolled_back"
    assert not (source / "nested").exists()


def test_source_apply_source_root_replacement_writes_neither_tree(
    tmp_path,
    monkeypatch,
):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)
    context.project_state_root = tmp_path / "apply-state"
    detached = tmp_path / "detached-source"
    swaps = _record_rename_swaps(monkeypatch)

    def replace_source_root(stage, _path):
        if stage == "after_journal":
            source.rename(detached)
            source.mkdir()
            (source / "a.txt").write_text("replacement\n", encoding="utf-8")

    result = SourceApplier(
        context,
        observer,
        fault_injector=replace_source_root,
    ).apply(diff_digest)

    assert result["status"] == "apply_review_required"
    assert swaps == []
    assert (detached / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert (source / "a.txt").read_text(encoding="utf-8") == "replacement\n"
    manifest = json.loads(
        (context.sandbox_state_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["state"] == "review_required"
    with pytest.raises(SandboxSessionError, match="sandbox_manifest_invalid"):
        SourceApplier(context, observer).apply(diff_digest)


def test_source_apply_root_replacement_terminalizes_bound_session_for_review(
    tmp_path,
):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    detached = tmp_path / "detached-source"

    def replace_source_root(stage, _path):
        if stage == "after_journal":
            source.rename(detached)
            source.mkdir()
            (source / "a.txt").write_text("replacement\n", encoding="utf-8")

    result = SourceApplier(
        context,
        observer,
        fault_injector=replace_source_root,
    ).apply(diff["diff_digest"])

    manifest = json.loads(
        (context.sandbox_state_root / "manifest.json").read_text(encoding="utf-8")
    )
    guard = CheckpointStore(detached).source_apply_guard()
    assert result["status"] == "apply_review_required"
    assert manifest["state"] == "review_required"
    assert manifest["apply"] == {
        "journal_id": result["journal_id"],
        "status": "apply_review_required",
    }
    assert guard == {
        "record_type": "docker_sandbox_source_apply_guard",
        "format_version": 1,
        "journal_id": result["journal_id"],
        "sandbox_id": manifest["sandbox_id"],
        "diff_digest": diff["diff_digest"],
    }
    store = context.runner.session_store
    authority = read_source_apply_authority(store.parent, source)
    assert authority["journal_id"] == result["journal_id"]
    assert authority["sandbox_id"] == manifest["sandbox_id"]
    assert authority["state_root"] == str(context.sandbox_state_root)
    journal = SourceApplyStore(context.sandbox_state_root).load_journal(
        result["journal_id"],
        sandbox_id=manifest["sandbox_id"],
    )
    assert journal["status"] == "apply_review_required"
    with pytest.raises(SandboxSessionError, match="sandbox_manifest_invalid"):
        store.inspect(context.sandbox_state_root)
    assert (
        find_project_sandbox_session(
            source / ".pico",
            source,
            manifest["pico_session_id"],
        )
        is None
    )


def test_external_apply_authority_blocks_host_mutation_after_root_replacement(
    tmp_path,
):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    detached = tmp_path / "detached-source"

    def replace_source_root(stage, _path):
        if stage == "after_journal":
            source.rename(detached)
            source.mkdir()
            (source / "a.txt").write_text("replacement\n", encoding="utf-8")

    SourceApplier(context, observer, fault_injector=replace_source_root).apply(
        diff["diff_digest"]
    )
    host_store = CheckpointStore(
        source,
        source_apply_authority=lambda: read_source_apply_authority(
            context.runner.session_store.parent,
            source,
        ),
    )

    with pytest.raises(CheckpointStoreError, match="source_apply_review_required"):
        with host_store.mutation_lock():
            pass

    assert (source / "a.txt").read_text(encoding="utf-8") == "replacement\n"
    assert (detached / "a.txt").read_text(encoding="utf-8") == "before\n"


def test_external_apply_authority_closes_pre_journal_crash_window(tmp_path):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)

    def crash_after_reservation(stage, _path):
        if stage == "after_reservation":
            raise KeyboardInterrupt("crash")

    with pytest.raises(KeyboardInterrupt, match="crash"):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_reservation,
        ).apply(diff_digest)

    authority = read_source_apply_authority(
        context.runner.session_store.parent,
        source,
    )
    assert authority is not None
    assert not list(
        SourceApplyStore(context.source_apply_state_root).journals.glob(
            "apply_*.json"
        )
    )
    assert context.current_session().state == "pending_review"

    with pytest.raises(CheckpointStoreError, match="source_apply_review_required"):
        with CheckpointStore(
            source,
            source_apply_authority=lambda: read_source_apply_authority(
                context.runner.session_store.parent,
                source,
            ),
        ).mutation_lock():
            pass

    result = SourceApplier(context, observer).apply(diff_digest)
    assert result["status"] == "apply_applied"
    assert result["journal_id"] == authority["journal_id"]
    assert read_source_apply_authority(
        context.runner.session_store.parent,
        source,
    ) is None


@pytest.mark.parametrize("changed_root", ("source", "staging"))
def test_reservation_only_conflict_clears_exact_authority(
    tmp_path,
    changed_root,
):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)

    def crash_after_reservation(stage, _path):
        if stage == "after_reservation":
            raise KeyboardInterrupt("crash")

    with pytest.raises(KeyboardInterrupt, match="crash"):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_reservation,
        ).apply(diff_digest)
    authority = read_source_apply_authority(
        context.runner.session_store.parent,
        source,
    )
    changed = source if changed_root == "source" else context.execution_root
    (changed / "a.txt").write_text("changed after reservation\n", encoding="utf-8")

    result = SourceApplier(context, observer).apply(diff_digest)

    assert result == {
        "status": "apply_conflicted",
        "diff_digest": diff_digest,
        "journal_id": "",
    }
    assert context.current_session().manifest["apply"] == {
        "journal_id": "",
        "status": "apply_conflicted",
    }
    assert read_source_apply_authority(
        context.runner.session_store.parent,
        source,
    ) is None
    assert not list(
        SourceApplyStore(context.source_apply_state_root).journals.glob(
            "apply_*.json"
        )
    )
    assert CheckpointStore(source).source_apply_guard() is None
    assert authority["journal_id"]


def test_external_apply_authority_tamper_blocks_without_docker(tmp_path):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)

    def crash_after_guard(stage, _path):
        if stage == "after_guard":
            raise KeyboardInterrupt("crash")

    with pytest.raises(KeyboardInterrupt):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_guard,
        ).apply(diff_digest)
    authority = read_source_apply_authority(
        context.runner.session_store.parent,
        source,
    )
    clear_source_apply_authority(
        context.runner.session_store.parent,
        source,
        expected_authority=authority,
    )
    control = next(
        context.sandbox_state_root.parent.glob(".source-apply-control")
    )
    (control / "active.json").write_bytes(b"{invalid")
    (control / "active.json").chmod(0o600)

    with pytest.raises(SandboxSessionError, match="sandbox_state_invalid"):
        read_source_apply_authority(context.runner.session_store.parent, source)


def test_external_apply_authority_cleanup_rejects_bound_field_tamper(tmp_path):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)

    def crash_after_guard(stage, _path):
        if stage == "after_guard":
            raise KeyboardInterrupt("crash")

    with pytest.raises(KeyboardInterrupt):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_guard,
        ).apply(diff_digest)
    authority = read_source_apply_authority(
        context.runner.session_store.parent,
        source,
    )

    with pytest.raises(SandboxSessionError, match="sandbox_state_invalid"):
        clear_source_apply_authority(
            context.runner.session_store.parent,
            source,
            expected_authority={
                **authority,
                "diff_digest": "sha256:" + "f" * 64,
            },
        )

    assert read_source_apply_authority(
        context.runner.session_store.parent,
        source,
    ) == authority


def test_external_apply_authority_cleanup_requires_present_exact_record(tmp_path):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)

    def crash_after_guard(stage, _path):
        if stage == "after_guard":
            raise KeyboardInterrupt("crash")

    with pytest.raises(KeyboardInterrupt):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_guard,
        ).apply(diff_digest)
    parent = context.runner.session_store.parent
    authority = read_source_apply_authority(parent, source)
    control = next(context.sandbox_state_root.parent.glob(".source-apply-control"))
    active = control / "active.json"
    original = active.read_bytes()
    tampered = json.loads(original)
    tampered["source_inode"] += 1
    active.write_text(json.dumps(tampered), encoding="utf-8")
    active.chmod(0o600)

    with pytest.raises(SandboxSessionError, match="sandbox_state_invalid"):
        clear_source_apply_authority(
            parent,
            source,
            expected_authority=authority,
        )

    active.write_bytes(original)
    active.chmod(0o600)
    active.unlink()
    with pytest.raises(SandboxSessionError, match="sandbox_state_invalid"):
        clear_source_apply_authority(
            parent,
            source,
            expected_authority=authority,
        )


def test_external_apply_authority_cleanup_rejects_control_directory_replacement(
    tmp_path,
    monkeypatch,
):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)

    def crash_after_guard(stage, _path):
        if stage == "after_guard":
            raise KeyboardInterrupt("crash")

    with pytest.raises(KeyboardInterrupt):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_guard,
        ).apply(diff_digest)
    parent = context.runner.session_store.parent
    authority = read_source_apply_authority(parent, source)
    control = next(context.sandbox_state_root.parent.glob(".source-apply-control"))
    detached = control.with_name(".source-apply-control-detached")
    original_open = os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and Path(path) == control and flags & os.O_DIRECTORY:
            replaced = True
            control.rename(detached)
            control.mkdir(mode=0o700)
            (control / "active.json").write_bytes(
                (detached / "active.json").read_bytes()
            )
            (control / "active.json").chmod(0o600)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises(SandboxSessionError, match="sandbox_state_invalid"):
        clear_source_apply_authority(
            parent,
            source,
            expected_authority=authority,
        )

    assert replaced is True
    assert (detached / "active.json").is_file()
    with pytest.raises(SandboxSessionError, match="sandbox_state_invalid"):
        read_source_apply_authority(parent, source)


def test_external_apply_authority_blocks_agent_start_before_state_creation(
    tmp_path,
    monkeypatch,
):
    state_home = tmp_path / ".pico"
    state_home.mkdir()
    source, context, observer, diff_digest = _modified_candidate(state_home)

    def crash_after_guard(stage, _path):
        if stage == "after_guard":
            raise KeyboardInterrupt("crash")

    with pytest.raises(KeyboardInterrupt):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_guard,
        ).apply(diff_digest)
    detached = tmp_path / "detached-source"
    source.rename(detached)
    source.mkdir()
    (source / "README.md").write_text("replacement\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    args = pico_cli.build_arg_parser().parse_args(["--cwd", str(source)])

    with pytest.raises(SandboxSessionError, match="source_apply_review_required"):
        pico_cli.build_agent(args)

    assert not (source / ".pico").exists()


@pytest.mark.parametrize(
    "outcome",
    ("apply_applied", "apply_failed_rolled_back"),
)
def test_finish_apply_nonreview_outcomes_reject_source_root_replacement(
    tmp_path,
    outcome,
):
    project_state = tmp_path / "project-state"
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
        project_state_root=project_state,
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    store = context.runner.session_store
    journal_id = "apply_" + "a" * 32
    store.begin_apply(
        context.sandbox_state_root,
        diff_digest=diff["diff_digest"],
        journal_id=journal_id,
    )
    source.rename(tmp_path / "detached-source")
    source.mkdir()

    with pytest.raises(SandboxSessionError, match="sandbox_manifest_invalid"):
        store.finish_apply(
            context.sandbox_state_root,
            journal_id=journal_id,
            outcome=outcome,
        )

    manifest = json.loads(
        (context.sandbox_state_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["state"] == "applying"


def test_finish_apply_review_outcome_still_rejects_sidecar_tamper(tmp_path):
    project_state = tmp_path / "project-state"
    _source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
        project_state_root=project_state,
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    store = context.runner.session_store
    journal_id = "apply_" + "b" * 32
    store.begin_apply(
        context.sandbox_state_root,
        diff_digest=diff["diff_digest"],
        journal_id=journal_id,
    )
    sidecar_path = Path(context.sandbox_session.manifest["sidecar"]["path"])
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["state_inode"] += 1
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    sidecar_path.chmod(0o600)

    with pytest.raises(SandboxSessionError, match="sandbox_manifest_invalid"):
        store.finish_apply(
            context.sandbox_state_root,
            journal_id=journal_id,
            outcome="apply_review_required",
        )

    manifest = json.loads(
        (context.sandbox_state_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["state"] == "applying"
    with pytest.raises(SandboxSessionError, match="sandbox_manifest_invalid"):
        context.current_session()


def test_source_apply_reconcile_rejects_corrupted_active_journal(tmp_path):
    source, context, observer, diff_digest = _modified_candidate(tmp_path)

    def corrupt_journal(stage, _path):
        if stage != "after_journal":
            return
        journal_id = context.current_session().manifest["apply"]["journal_id"]
        path = SourceApplyStore(context.source_apply_state_root).journals / (
            f"{journal_id}.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["status"] = []
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=corrupt_journal,
        ).apply(diff_digest)

    assert (source / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert context.current_session().state == "applying"
    with pytest.raises(SandboxApplyError, match="sandbox_apply_journal_invalid"):
        SourceApplier(context, observer).reconcile()
    assert context.current_session().state == "applying"
    with pytest.raises(SandboxApplyError, match="sandbox_apply_not_allowed"):
        SourceApplier(context, observer).apply(diff_digest)


def test_empty_directory_only_is_not_an_apply_candidate(tmp_path):
    source, context, _blobs, observer, _baseline = _observer(tmp_path, {})
    (context.execution_root / "empty").mkdir()
    applier = SourceApplier(context, observer)
    source_before = [
        item for item in _source_worktree_snapshot(source) if item[0] != "."
    ]

    result = observer.finalize_diff(lambda text: text)

    assert result["artifact"]["entries"] == []
    assert result["artifact"]["candidate_bytes"] == 0
    current = context.current_session()
    assert current.manifest["diff"]["candidate_count"] == 0
    applied = applier.apply(result["diff_digest"])
    journal = SourceApplyStore(context.source_apply_state_root).load_journal(
        applied["journal_id"]
    )
    assert applied["status"] == "apply_applied"
    assert journal["entries"] == []
    assert journal["created_dirs"] == []
    assert [
        item for item in _source_worktree_snapshot(source) if item[0] != "."
    ] == source_before
    assert context.current_session().state == "applied"
    assert not (source / "empty").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("status", []),
        lambda value: value.__setitem__("journal_id", []),
        lambda value: value["source"].__setitem__("root", []),
        lambda value: value["entries"][0].__setitem__("before_blob_ref", "0" * 64),
        lambda value: value["entries"][0]["before_identity"].__setitem__(
            "inode", 0
        ),
        lambda value: value["entries"][0]["prepared_identity"].__setitem__(
            "inode", 0
        ),
        lambda value: value["entries"][0]["after_identity"].__setitem__(
            "inode", 0
        ),
        lambda value: value["entries"][0].__setitem__("change_kind", []),
        lambda value: value["entries"][0].__setitem__("status", "pending"),
        lambda value: value["entries"][0].__setitem__(
            "temp_name",
            ".pico-apply-" + "f" * 32 + "-" + "0" * 16 + ".tmp",
        ),
    ],
)
def test_source_apply_journal_rejects_tampered_types_and_bindings(
    tmp_path,
    mutate,
):
    _source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    result = SourceApplier(context, observer).apply(diff["diff_digest"])
    journal = SourceApplyStore(context.source_apply_state_root).load_journal(
        result["journal_id"],
        sandbox_id=context.sandbox_session.sandbox_id,
    )
    tampered = deepcopy(journal)
    mutate(tampered)

    with pytest.raises(SandboxApplyError, match="sandbox_apply_journal_invalid"):
        _validate_apply_journal(tampered)


def test_source_apply_store_rejects_tampered_before_blob(tmp_path):
    _source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_journal(stage, _path):
        if stage == "after_journal":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_journal,
        ).apply(diff["diff_digest"])

    store = SourceApplyStore(context.source_apply_state_root)
    journal_id = context.current_session().manifest["apply"]["journal_id"]
    journal = store.load_journal(journal_id)
    blob_ref = journal["entries"][0]["before_blob_ref"]
    blob_path = store.blobs / blob_ref[:2] / blob_ref
    blob_path.write_bytes(b"tampered\n")
    blob_path.chmod(0o600)

    with pytest.raises(SandboxApplyError, match="sandbox_apply_journal_invalid"):
        store.load_journal(journal_id)


def test_source_apply_blob_cleanup_preserves_refs_used_by_active_journal(tmp_path):
    _source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_journal(stage, _path):
        if stage == "after_journal":
            raise _SimulatedCrash()

    with pytest.raises(_SimulatedCrash):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_journal,
        ).apply(diff["diff_digest"])

    store = SourceApplyStore(context.source_apply_state_root)
    active_id = context.current_session().manifest["apply"]["journal_id"]
    active = store.load_journal(active_id)
    terminal = deepcopy(active)
    terminal_id = "apply_" + "f" * 32
    terminal["journal_id"] = terminal_id
    terminal["status"] = "apply_failed_rolled_back"
    for entry in terminal["entries"]:
        entry["status"] = "rolled_back"
        entry["temp_name"] = (
            f".pico-apply-{terminal_id[6:]}-"
            + hashlib.sha256(entry["path"].encode("utf-8")).hexdigest()[:16]
            + ".tmp"
        )
    store.write_journal(terminal)
    blob_ref = active["entries"][0]["before_blob_ref"]
    canonical = store.blobs / blob_ref[:2] / blob_ref
    trash = store.root / "trash-blobs"
    trash.mkdir(mode=0o700)
    (trash / blob_ref).write_bytes(canonical.read_bytes())
    (trash / blob_ref).chmod(0o600)

    cleanup = store.cleanup_terminal_blobs(terminal_id)

    assert cleanup["complete"] is True
    assert cleanup["protected_count"] == 1
    assert canonical.is_file()
    assert not trash.exists()


def test_source_apply_blob_cleanup_failure_preserves_staging_for_retry(
    tmp_path,
    monkeypatch,
):
    source, context, _blobs, observer, _baseline = _observer(
        tmp_path,
        {"a.txt": "before\n"},
    )
    (context.execution_root / "a.txt").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    monkeypatch.setattr(
        SourceApplyStore,
        "cleanup_terminal_blobs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SandboxApplyError("sandbox_apply_cleanup_failed")
        ),
    )

    result = SourceApplier(context, observer).apply(diff["diff_digest"])

    assert result["status"] == "applied_cleanup_pending"
    assert context.current_session().state == "applied"
    assert context.execution_root.is_dir()
    assert CheckpointStore(source).source_apply_guard() is not None
