# Pico A2 Recovery Integrity and Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fail-closed, private, durable checkpoint and restore path with one net file entry per path, owner-safe mutation serialization, crash-reconcilable restore journals, and an explicit Recovery Review CLI.

**Architecture:** Keep the current owners: `CheckpointStore` validates and persists records/blobs, `ToolChangeRecorder` owns Tool Change transitions, `RecoveryCheckpointWriter` coalesces turn history, and `RecoveryManager` plans/applies/reconciles restores. A single cross-process mutation lock serializes Pico workspace and memory mutations; exact restore bytes remain unredacted only after path/content eligibility and are stored privately and durably.

**Tech Stack:** Python 3.11+, standard library only, pytest, Ruff, POSIX/macOS `fcntl`, JSON checkpoint schema v1 with additive fields.

## Global Constraints

- Execute in `/Users/wei/Desktop/pico/.worktrees/action-kernel-messages-v3` on branch `codex/action-kernel-messages-v3`.
- A1 must already provide these exact interfaces in `pico/security.py`: `is_sensitive_path(path) -> bool`, `contains_secret_material(text, env=None, secret_env_names=None) -> bool`, `ensure_private_dir(path) -> Path`, and `ensure_private_file(path) -> Path`.
- A1 freezes `CheckpointStore(workspace_root, redactor=None)`, `CheckpointStore.set_redactor(redactor)`, and `locked_file(path, *, require_lock=False)`. A2 preserves these signatures and their no-follow/private semantics.
- Do not add third-party dependencies, a policy class hierarchy, a recovery framework, or an OS sandbox.
- Keep `checkpoint-record-v1`, `tool-change-record-v1`, and `restore-plan-v1`; new fields are additive.
- Never redact exact restore blob bytes. Sensitive path/content is ineligible and produces no blob.
- Every checkpoint/tool-change/journal/quarantine metadata JSON write and RMW applies the configured redactor to a deep copy before persistence and returns the same safe copy. Exact blob bytes and quarantined invalid raw bytes are never redacted.
- All `.pico/checkpoints` directories are 0700 and all records, blobs, locks, temp files, and quarantine files are 0600 on POSIX.
- A mutation must fail closed when a real cross-process lock is unavailable. Windows locking is outside this local macOS stage.
- Lock order is always mutation lock, then store lock.
- Approval completes before the mutation lock is acquired. The lock covers guard, prepared state, Tool Change start, runner, after-capture, and finalize.
- `apply_restore()` holds the same mutation lock across strict plan rebuild, pre-state capture, blob durability, applying journal creation, every mutation/outcome RMW, and terminal journal RMW.
- No production edit is allowed before its focused test has been run and observed failing for the intended missing behavior.
- Each task ends with focused tests, relevant Ruff, `git diff --check`, a spec-compliance review, a code-quality review, and its own commit.

## File and Record Map

- `pico/file_lock.py`: A1-frozen no-follow private POSIX cross-process file locking, reverified by A2 adversarial tests.
- `pico/checkpoint_store.py`: private layout, ID/ref validation, durable blob/JSON writes, atomic RMW, strict enumeration, quarantine, and prune.
- `pico/recovery_paths.py`: lexical workspace path validation without following symlinks.
- `pico/recovery_policy.py`: eligibility for a specific path and the exact bytes already read.
- `pico/recovery_models.py`: additive record defaults and stable lifecycle constants.
- `pico/tool_change_recorder.py`: owner-safe pending-to-terminal transitions and Tool Change resolution.
- `pico/tool_executor.py`: one mutation gate and complete FileEntry capture.
- `pico/runtime.py`: owner creation, no startup auto-interruption, and pending review status.
- `pico/recovery_checkpoint_writer.py`: one net FileEntry per path and restore audit creation.
- `pico/recovery_manager.py`: strict plan, durable apply, partial-state journal, reconciliation, and undo source.
- `pico/cli_recovery.py`: pending/review/quarantine commands and nonzero restore outcomes.
- `pico/cli.py`: `COMMAND_SPECS` registration for the Recovery Review subcommands.

The additive FileEntry contract is:

```python
FILE_ENTRY_KEYS = {
    "path",
    "change_kind",
    "snapshot_eligible",
    "ineligible_reason",
    "before_exists",
    "before_blob_ref",
    "before_hash",
    "before_mode",
    "after_exists",
    "after_blob_ref",
    "after_hash",
    "after_mode",
    "expected_current_hash",
    "source_tool_change_ids",
}
```

Restore journal intent fields are:

```python
{
    "path": "src/app.py",
    "pre_state": {"exists": True, "hash": "sha256", "blob_ref": "sha256", "mode": 0o644},
    "planned_post_state": {"exists": True, "hash": "sha256", "blob_ref": "sha256", "mode": 0o644},
    "outcome": "pending",
    "reason": "",
    "target_modified": False,
    "actual_post_state": {},
}
```

---

### Task 1: Checkpoint Store ID, Blob, and Private-Path Trust Boundary

**Depends on:** A1 security interfaces.

**Files:**
- Modify: `pico/checkpoint_store.py`
- Modify: `pico/recovery_models.py`
- Modify: `pico/recovery_checkpoint_writer.py`
- Create: `tests/test_checkpoint_store_security.py`
- Modify: `tests/test_recovery_models.py`

**Interfaces:**
- Consumes: `ensure_private_dir(path) -> Path`, `ensure_private_file(path) -> Path`, and A1's redactor contract.
- Preserves: `CheckpointStore(workspace_root, redactor=None)` and `CheckpointStore.set_redactor(redactor)`.
- Produces: `CheckpointStoreError(code: str, message: str)`, complete schema/internal-ID/status validation, validated existing store methods, and hash-verifying `read_blob(blob_ref: str) -> bytes`.

- [ ] **Step 1: Write the failing trust-boundary tests**

```python
import json
import os
import stat

import pytest

from pico.checkpoint_store import CheckpointStore
from pico.recovery_models import new_checkpoint_record, new_tool_change_record


def checkpoint_record(tmp_path, checkpoint_id):
    return new_checkpoint_record(
        checkpoint_id,
        "turn",
        "session",
        "run",
        "turn",
        "",
        str(tmp_path.resolve()),
    )


@pytest.mark.parametrize("record_id", ["", ".", "..", "../escape", "a/b", "a\\b"])
def test_record_ids_reject_unsafe_names(tmp_path, record_id):
    store = CheckpointStore(tmp_path)
    record = checkpoint_record(tmp_path, "ckpt_safe")
    record["checkpoint_id"] = record_id
    with pytest.raises(ValueError):
        store.write_checkpoint_record(record)


@pytest.mark.parametrize("blob_ref", ["abc", "A" * 64, "g" * 64, "../" + "a" * 64])
def test_blob_refs_require_lowercase_sha256(tmp_path, blob_ref):
    store = CheckpointStore(tmp_path)
    with pytest.raises(ValueError):
        store.read_blob(blob_ref)


def test_read_blob_rejects_hash_mismatch(tmp_path):
    store = CheckpointStore(tmp_path)
    info = store.write_blob(b"trusted")
    store._blob_path(info["blob_ref"]).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="blob_hash_mismatch"):
        store.read_blob(info["blob_ref"])


def test_store_rejects_symlinked_records_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / ".pico" / "checkpoints"
    root.mkdir(parents=True)
    os.symlink(outside, root / "records")

    with pytest.raises(ValueError, match="symlink"):
        CheckpointStore(tmp_path)


def test_store_layout_uses_private_modes(tmp_path):
    store = CheckpointStore(tmp_path)
    record = checkpoint_record(tmp_path, "ckpt_private")
    path = store.write_checkpoint_record(record)
    blob = store.write_blob(b"private")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store._blob_path(blob["blob_ref"]).stat().st_mode) == 0o600
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("schema_version", "checkpoint-record-v0", "unsupported_schema"),
        ("checkpoint_type", "unknown", "invalid_checkpoint_type"),
        ("status", "mystery", "invalid_status"),
    ],
)
def test_checkpoint_record_rejects_invalid_schema_type_and_status(tmp_path, field, value, code):
    store = CheckpointStore(tmp_path)
    record = checkpoint_record(tmp_path, "ckpt_invalid")
    record[field] = value
    with pytest.raises(ValueError, match=code):
        store.write_checkpoint_record(record)


def test_load_rejects_internal_id_mismatch(tmp_path):
    store = CheckpointStore(tmp_path)
    record = checkpoint_record(tmp_path, "ckpt_internal")
    path = store.records_dir / "ckpt_requested.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="internal_id_mismatch"):
        store.load_checkpoint_record("ckpt_requested")


def test_tool_change_record_requires_schema_status_and_internal_id(tmp_path):
    store = CheckpointStore(tmp_path)
    record = new_tool_change_record(
        "tc_internal", "", "turn", "write_file", "workspace_write", "owner"
    )
    path = store.tool_changes_dir / "tc_requested.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError, match="internal_id_mismatch"):
        store.load_tool_change_record("tc_requested")


def test_record_schema_rejects_wrong_container_and_scalar_types(tmp_path):
    store = CheckpointStore(tmp_path)
    checkpoint = checkpoint_record(tmp_path, "ckpt_shape")
    checkpoint["file_entries"] = {}
    with pytest.raises(ValueError, match="invalid_record_shape"):
        store.write_checkpoint_record(checkpoint)

    tool_change = new_tool_change_record(
        "tc_shape", "", "turn", "write_file", "workspace_write", "owner"
    )
    tool_change["owner_id"] = []
    with pytest.raises(ValueError, match="invalid_record_shape"):
        store.write_tool_change_record(tool_change)


def test_json_is_redacted_but_blob_bytes_remain_exact(tmp_path):
    sentinel = "sk-checkpoint-redactor-sentinel"

    def redact(value):
        return json.loads(json.dumps(value).replace(sentinel, "<redacted>"))

    store = CheckpointStore(tmp_path, redactor=redact)
    record = checkpoint_record(tmp_path, "ckpt_redacted")
    record["verification_evidence"] = [{"stdout_tail": sentinel}]
    path = store.write_checkpoint_record(record)
    blob = store.write_blob(sentinel.encode())

    assert sentinel.encode() not in path.read_bytes()
    assert store.read_blob(blob["blob_ref"]) == sentinel.encode()

    store.set_redactor(lambda value: value)
    assert store.load_checkpoint_record("ckpt_redacted")["verification_evidence"][0]["stdout_tail"] == "<redacted>"


def test_legacy_v1_records_receive_only_additive_defaults(tmp_path):
    store = CheckpointStore(tmp_path)
    checkpoint = checkpoint_record(tmp_path, "ckpt_legacy")
    for key in ("status", "owner_id", "reviewed_at", "review_reason", "reviewed_by", "integrity_errors"):
        checkpoint.pop(key, None)
    (store.records_dir / "ckpt_legacy.json").write_text(json.dumps(checkpoint), encoding="utf-8")

    tool_change = new_tool_change_record(
        "tc_legacy", "", "turn", "write_file", "workspace_write", "owner"
    )
    for key in ("status", "owner_id", "prepared_file_entries", "recovery_context", "reviewed_at", "review_reason", "reviewed_by"):
        tool_change.pop(key, None)
    (store.tool_changes_dir / "tc_legacy.json").write_text(json.dumps(tool_change), encoding="utf-8")

    loaded_checkpoint = store.load_checkpoint_record("ckpt_legacy")
    loaded_tool = store.load_tool_change_record("tc_legacy")
    assert loaded_checkpoint["status"] == ""
    assert loaded_checkpoint["integrity_errors"] == []
    assert loaded_tool["status"] == "pending"
    assert loaded_tool["owner_id"] == ""
    assert loaded_tool["prepared_file_entries"] == []
    assert loaded_tool["recovery_context"] == {}


def test_restore_v1_without_additive_status_defaults_to_applied(tmp_path):
    store = CheckpointStore(tmp_path)
    record = checkpoint_record(tmp_path, "ckpt_restore_legacy")
    record["checkpoint_type"] = "restore"
    record.pop("status", None)
    (store.records_dir / "ckpt_restore_legacy.json").write_text(json.dumps(record), encoding="utf-8")
    assert store.load_checkpoint_record("ckpt_restore_legacy")["status"] == "applied"


def test_additive_defaulting_never_overwrites_present_wrong_type(tmp_path):
    store = CheckpointStore(tmp_path)
    record = checkpoint_record(tmp_path, "ckpt_wrong_default")
    record["integrity_errors"] = {}
    with pytest.raises(ValueError, match="invalid_record_shape"):
        store.write_checkpoint_record(record)
```

- [ ] **Step 2: Run the tests and confirm the intended RED state**

Run:

```bash
./.venv/bin/python -m pytest tests/test_checkpoint_store_security.py -q
```

Expected: unsafe IDs are accepted, tampered blob bytes are returned, record schema/internal identity/status is unchecked, JSON is not redacted, the symlinked directory is followed, or private mode assertions fail. The test run must fail because trust-boundary behavior is absent, not because of a syntax/import error.

- [ ] **Step 3: Add the minimal validation and private-layout implementation**

Add these definitions to `pico/checkpoint_store.py` and route `_record_path`, `_tool_change_path`, `_blob_path`, store initialization, all writes, and `read_blob` through them:

```python
import copy
import hashlib
import os
import re
import stat

from pico.security import ensure_private_dir, ensure_private_file, require_regular_no_symlink


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_BLOB_REF = re.compile(r"[0-9a-f]{64}\Z")
_CHECKPOINT_TYPES = {"turn", "restore", "manual"}
_RESTORE_STATUSES = {"applying", "applied", "blocked", "failed", "partial", "noop"}
_TOOL_CHANGE_STATUSES = {"pending", "finalized", "error", "partial_success", "interrupted"}


class CheckpointStoreError(ValueError):
    def __init__(self, code, message):
        self.code = str(code)
        super().__init__(self.code + ": " + str(message))


def _safe_id(value, label):
    text = str(value or "")
    if text in {"", ".", ".."} or _SAFE_ID.fullmatch(text) is None:
        raise CheckpointStoreError("invalid_record_id", f"invalid {label}")
    return text


def _blob_ref(value):
    text = str(value or "")
    if _BLOB_REF.fullmatch(text) is None:
        raise CheckpointStoreError("invalid_blob_ref", "invalid blob ref")
    return text


def _reject_non_regular_or_symlink(path, *, allow_missing):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise
    if stat.S_ISLNK(info.st_mode):
        raise CheckpointStoreError("symlink", f"symlink store path: {path.name}")
    if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
        raise CheckpointStoreError("invalid_store_path", f"invalid store path: {path.name}")


def _verified_blob_bytes(path, blob_ref):
    _reject_non_regular_or_symlink(path, allow_missing=False)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != blob_ref:
        raise CheckpointStoreError("blob_hash_mismatch", "blob hash mismatch")
    return data


def _with_additive_defaults(record, *, kind):
    if not isinstance(record, dict):
        return record
    result = copy.deepcopy(record)
    if kind == "checkpoint" and result.get("schema_version") == "checkpoint-record-v1":
        result.setdefault(
            "status",
            "applied" if result.get("checkpoint_type") == "restore" else "",
        )
        result.setdefault("owner_id", "")
        result.setdefault("reviewed_at", "")
        result.setdefault("review_reason", "")
        result.setdefault("reviewed_by", "")
        result.setdefault("integrity_errors", [])
    if kind == "tool_change" and result.get("schema_version") == "tool-change-record-v1":
        result.setdefault("status", "pending")
        result.setdefault("owner_id", "")
        result.setdefault("prepared_file_entries", [])
        result.setdefault("recovery_context", {})
        result.setdefault("reviewed_at", "")
        result.setdefault("review_reason", "")
        result.setdefault("reviewed_by", "")
    return result


def _validate_checkpoint_record(record, expected_id=None):
    if not isinstance(record, dict):
        raise CheckpointStoreError("invalid_record", "checkpoint record must be an object")
    if record.get("schema_version") != "checkpoint-record-v1":
        raise CheckpointStoreError("unsupported_schema", "unsupported checkpoint schema")
    checkpoint_id = _safe_id(record.get("checkpoint_id"), "checkpoint id")
    if expected_id is not None and checkpoint_id != expected_id:
        raise CheckpointStoreError("internal_id_mismatch", "checkpoint internal id mismatch")
    checkpoint_type = record.get("checkpoint_type")
    if checkpoint_type not in _CHECKPOINT_TYPES:
        raise CheckpointStoreError("invalid_checkpoint_type", "invalid checkpoint type")
    status = str(record.get("status") or "")
    if checkpoint_type == "restore" and status not in _RESTORE_STATUSES:
        raise CheckpointStoreError("invalid_status", "invalid restore status")
    if checkpoint_type != "restore" and status:
        raise CheckpointStoreError("invalid_status", "non-restore checkpoint has status")
    string_fields = (
        "status", "created_at", "session_id", "run_id", "turn_id", "parent_checkpoint_id", "workspace_root",
        "owner_id", "reviewed_at", "review_reason", "reviewed_by",
    )
    list_fields = (
        "tool_change_ids", "missing_tool_change_ids", "file_entries",
        "verification_evidence", "integrity_errors",
    )
    dict_fields = ("git_review_context", "restore_provenance")
    if any(not isinstance(record.get(key), str) for key in string_fields):
        raise CheckpointStoreError("invalid_record_shape", "invalid checkpoint string field")
    if any(not isinstance(record.get(key), list) for key in list_fields):
        raise CheckpointStoreError("invalid_record_shape", "invalid checkpoint list field")
    if any(not isinstance(record.get(key), dict) for key in dict_fields):
        raise CheckpointStoreError("invalid_record_shape", "invalid checkpoint object field")
    for tool_change_id in record["tool_change_ids"] + record["missing_tool_change_ids"]:
        _safe_id(tool_change_id, "tool change id")
    return record


def _validate_tool_change_record(record, expected_id=None):
    if not isinstance(record, dict):
        raise CheckpointStoreError("invalid_record", "tool change record must be an object")
    if record.get("schema_version") != "tool-change-record-v1":
        raise CheckpointStoreError("unsupported_schema", "unsupported tool change schema")
    tool_change_id = _safe_id(record.get("tool_change_id"), "tool change id")
    if expected_id is not None and tool_change_id != expected_id:
        raise CheckpointStoreError("internal_id_mismatch", "tool change internal id mismatch")
    if record.get("status") not in _TOOL_CHANGE_STATUSES:
        raise CheckpointStoreError("invalid_status", "invalid tool change status")
    string_fields = (
        "checkpoint_id", "turn_id", "owner_id", "tool_name", "effect_class",
        "started_at", "ended_at", "reviewed_at", "review_reason", "reviewed_by",
    )
    list_fields = (
        "affected_paths", "file_entries", "prepared_file_entries",
        "shell_side_effects", "trace_event_ids",
    )
    dict_fields = ("input_summary", "recovery_context", "approval", "error")
    if any(not isinstance(record.get(key), str) for key in string_fields):
        raise CheckpointStoreError("invalid_record_shape", "invalid tool change string field")
    if any(not isinstance(record.get(key), list) for key in list_fields):
        raise CheckpointStoreError("invalid_record_shape", "invalid tool change list field")
    if any(not isinstance(record.get(key), dict) for key in dict_fields):
        raise CheckpointStoreError("invalid_record_shape", "invalid tool change object field")
    return record


def _safe_json_record(self, payload, *, kind, expected_id):
    validator = _validate_checkpoint_record if kind == "checkpoint" else _validate_tool_change_record
    payload = _with_additive_defaults(payload, kind=kind)
    validator(payload, expected_id=expected_id)
    structural_keys = (
        (
            "schema_version", "checkpoint_id", "checkpoint_type", "status",
            "owner_id", "reviewed_at", "reviewed_by",
        )
        if kind == "checkpoint"
        else ("schema_version", "tool_change_id", "status", "owner_id", "effect_class")
    )
    structural = {key: payload.get(key) for key in structural_keys}
    safe = self._redactor(copy.deepcopy(payload))
    validator(safe, expected_id=expected_id)
    if {key: safe.get(key) for key in structural_keys} != structural:
        raise CheckpointStoreError("redactor_structural_change", "redactor changed structural fields")
    return safe
```

Before every v1 validation on both read and write, call `_with_additive_defaults()` and validate the returned copy. Missing additive fields receive defaults; present fields are never overwritten and still fail on wrong type/value. Also add the complete defaults to new record constructors in `pico/recovery_models.py`:

```python
# new_checkpoint_record
"status": "",
"owner_id": "",
"reviewed_at": "",
"review_reason": "",
"reviewed_by": "",
"integrity_errors": [],

# new_tool_change_record
"prepared_file_entries": [],
"recovery_context": {},
"reviewed_at": "",
"review_reason": "",
"reviewed_by": "",
```

Until Task 9 introduces applying journals, the existing successful `create_restore_checkpoint()` sets `status="applied"` and `owner_id=""` before writing, so every intermediate commit remains schema-valid.

`_write_json_atomic(path, payload, *, kind=None, expected_id=None)` performs the single redaction pass: record JSON calls `_safe_json_record()` when `kind` is set; quarantine metadata and other JSON call the configured redactor on a deep copy. It durably writes and returns the exact safe object persisted. Callers never pre-redact and never apply the redactor twice.

Preserve A1's constructor exactly:

```python
def __init__(self, workspace_root, redactor=None):
    self._redactor = redactor or (lambda value: value)


def set_redactor(self, redactor):
    self._redactor = redactor or (lambda value: value)
```

For each store-owned directory, call `ensure_private_dir()` only after checking any existing component with `lstat()`. For each final record/blob, call `ensure_private_file()` after atomic replacement. Validate structural fields before redaction, apply the configured redactor to a deep copy for JSON writes/RMW, validate that structural fields remain unchanged, persist and return that safe copy. `write_blob()` and quarantine raw-byte moves bypass the redactor. Do not chmod ordinary workspace files.

- [ ] **Step 4: Run focused tests and existing store regressions**

```bash
./.venv/bin/python -m pytest tests/test_checkpoint_store_security.py tests/test_checkpoint_store_phase1.py tests/test_recovery_models.py tests/test_recovery_checkpoint_writer.py -q
./.venv/bin/python -m ruff check pico/checkpoint_store.py pico/recovery_models.py pico/recovery_checkpoint_writer.py tests/test_checkpoint_store_security.py tests/test_recovery_models.py
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 5: Review and commit Task 1**

Review constructor/setter compatibility, complete schema/type/status/internal-ID checks, redaction of JSON and RMW results, exact blob preservation, validation before path joining, blob rehashing, no-follow path handling, and absence of workspace source chmod.

```bash
git add pico/checkpoint_store.py pico/recovery_models.py pico/recovery_checkpoint_writer.py tests/test_checkpoint_store_security.py tests/test_recovery_models.py
git commit -m "feat(recovery): validate checkpoint store inputs"
```

---

### Task 2: Durable Atomic Writes, CAS RMW, and Required Mutation Lock

**Depends on:** Task 1.

**Files:**
- Modify: `pico/file_lock.py`
- Modify: `pico/checkpoint_store.py`
- Modify: `tests/test_file_lock.py`
- Create: `tests/test_checkpoint_store_durability.py`

**Interfaces:**
- Consumes and re-verifies A1's frozen `locked_file(path, *, require_lock=False)` no-follow/private contract.
- Produces: `CheckpointStore.mutation_lock()`, redacted durable RMW, `update_checkpoint_record()`, and `update_tool_change_record()`.

- [ ] **Step 1: Write failing durability and CAS tests**

```python
import os
import stat
from pathlib import Path

import pytest

from pico import file_lock
from pico.checkpoint_store import CheckpointStore
from pico.recovery_models import new_checkpoint_record, new_tool_change_record


def test_required_lock_fails_when_fcntl_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(file_lock, "fcntl", None)
    with pytest.raises(RuntimeError, match="cross-process lock unavailable"):
        with file_lock.locked_file(tmp_path / "required.lock", require_lock=True):
            raise AssertionError("lock body must not run")


def test_lock_rejects_parent_and_leaf_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent_link = tmp_path / "linked"
    parent_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        with file_lock.locked_file(parent_link / "store.lock", require_lock=True):
            raise AssertionError("symlinked parent lock body ran")

    real_target = outside / "target.lock"
    real_target.write_text("untouched", encoding="utf-8")
    leaf_link = tmp_path / "leaf.lock"
    leaf_link.symlink_to(real_target)
    with pytest.raises(ValueError, match="symlink"):
        with file_lock.locked_file(leaf_link, require_lock=True):
            raise AssertionError("symlinked leaf lock body ran")
    assert real_target.read_text(encoding="utf-8") == "untouched"


def test_lock_rejects_fifo_without_blocking(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO is unavailable")
    fifo = tmp_path / "store.lock"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError, match="regular"):
        with file_lock.locked_file(fifo, require_lock=True):
            raise AssertionError("FIFO lock body ran")


def test_lock_detects_inode_replacement_between_check_and_open(tmp_path, monkeypatch):
    lock_path = tmp_path / "store.lock"
    lock_path.write_text("original", encoding="utf-8")
    replacement = tmp_path / "replacement.lock"
    replacement.write_text("replacement", encoding="utf-8")
    original_inode = lock_path.stat().st_ino
    real_open = os.open
    swapped = {"done": False}

    def replace_then_open(path, flags, mode=0o777):
        if Path(path) == lock_path and not swapped["done"]:
            swapped["done"] = True
            lock_path.unlink()
            replacement.replace(lock_path)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", replace_then_open)
    with pytest.raises(ValueError, match="inode_changed"):
        with file_lock.locked_file(lock_path, require_lock=True):
            raise AssertionError("replaced inode lock body ran")
    assert lock_path.stat().st_ino != original_inode


def test_lock_hardens_existing_regular_file_to_0600(tmp_path):
    lock_path = tmp_path / "store.lock"
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o644)
    with file_lock.locked_file(lock_path, require_lock=True):
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_checkpoint_store_exposes_mutation_lock(tmp_path):
    store = CheckpointStore(tmp_path)
    with store.mutation_lock():
        assert store.mutation_lock_path.exists()
        assert stat.S_IMODE(store.mutation_lock_path.stat().st_mode) == 0o600


def test_checkpoint_rmw_rejects_status_conflict(tmp_path):
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_cas", "restore", "session", "run", "turn", "", str(tmp_path.resolve())
    )
    record["status"] = "applied"
    store.write_checkpoint_record(record)

    with pytest.raises(ValueError, match="status_conflict"):
        store.update_checkpoint_record(
            "ckpt_cas",
            lambda record: {**record, "status": "partial"},
            expected_status="applying",
        )

    assert store.load_checkpoint_record("ckpt_cas")["status"] == "applied"


def test_blob_and_json_writes_fsync_file_then_parent(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    events = []
    monkeypatch.setattr(store, "_fsync_file", lambda handle: events.append("file"))
    monkeypatch.setattr(store, "_fsync_parent", lambda path: events.append("parent"))

    store.write_blob(b"durable")
    store.write_checkpoint_record(new_checkpoint_record(
        "ckpt_durable", "turn", "session", "run", "turn", "", str(tmp_path.resolve())
    ))

    assert events == ["file", "parent", "file", "parent"]
```

- [ ] **Step 2: Run the tests and confirm RED**

```bash
./.venv/bin/python -m pytest tests/test_checkpoint_store_durability.py -q
```

Expected: no-follow/FIFO/inode/private-mode assertions expose any A1 regression, while missing `mutation_lock`, `_fsync_file`, `_fsync_parent`, and RMW interfaces produce focused failures.

- [ ] **Step 3: Implement the required lock and durable store primitives**

Preserve the A1 signature and ensure `pico/file_lock.py` has this complete no-follow implementation; do not replace it with `Path.open()`:

```python
@contextmanager
def locked_file(path, *, require_lock=False):
    path = Path(path)
    ensure_private_dir(path.parent)
    before = None
    try:
        before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        before = None
    if before is not None and not stat.S_ISREG(before.st_mode):
        kind = "symlink" if stat.S_ISLNK(before.st_mode) else "regular file required"
        raise ValueError(kind)
    if require_lock and fcntl is None:
        raise RuntimeError("cross-process lock unavailable")
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("regular file required")
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("inode_changed")
        if before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("inode_changed")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            descriptor = -1
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield handle
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
```

If `flock` fails, the `os.fdopen` context closes the descriptor and the function raises before yielding. The pre-open, `O_NOFOLLOW`, `fstat`, post-open path stat, inode equality, and `fchmod(0600)` checks are all mandatory.

Add these store methods and use `_write_json_atomic()` with file/parent fsync for all record writes:

```python
@contextmanager
def mutation_lock(self):
    with file_lock.locked_file(self.mutation_lock_path, require_lock=True):
        yield


def _fsync_file(self, handle):
    handle.flush()
    os.fsync(handle.fileno())


def _fsync_parent(self, path):
    descriptor = os.open(str(Path(path).parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def update_checkpoint_record(self, checkpoint_id, transform, *, expected_status=None):
    checkpoint_id = _safe_id(checkpoint_id, "checkpoint id")
    with file_lock.locked_file(self.lock_path, require_lock=True):
        record = self._load_checkpoint_record_unlocked(checkpoint_id)
        if expected_status is not None and record.get("status") != expected_status:
            raise CheckpointStoreError("status_conflict", "checkpoint status conflict")
        updated = transform(dict(record))
        return self._write_json_atomic(
            self._record_path(checkpoint_id),
            updated,
            kind="checkpoint",
            expected_id=checkpoint_id,
        )


def update_tool_change_record(self, tool_change_id, transform, *, expected_status=None):
    tool_change_id = _safe_id(tool_change_id, "tool change id")
    with file_lock.locked_file(self.lock_path, require_lock=True):
        record = self._load_tool_change_record_unlocked(tool_change_id)
        if expected_status is not None and record.get("status") != expected_status:
            raise CheckpointStoreError("status_conflict", "tool change status conflict")
        updated = transform(dict(record))
        return self._write_json_atomic(
            self._tool_change_path(tool_change_id),
            updated,
            kind="tool_change",
            expected_id=tool_change_id,
        )
```

Set `self.mutation_lock_path = self.root / ".mutation.lock"`. Private unlocked loaders must be called only while the store lock is held and must perform the same ID/type validation as public loaders.

- [ ] **Step 4: Run focused and store regression tests**

```bash
./.venv/bin/python -m pytest tests/test_file_lock.py tests/test_checkpoint_store_durability.py tests/test_checkpoint_store_security.py tests/test_checkpoint_store_phase1.py -q
./.venv/bin/python -m ruff check pico/file_lock.py pico/checkpoint_store.py tests/test_file_lock.py tests/test_checkpoint_store_durability.py
git diff --check
```

Expected: all commands exit 0 and no test observes an unlocked RMW.

- [ ] **Step 5: Review and commit Task 2**

Review A1 signature compatibility, parent/leaf no-follow checks, FIFO rejection before blocking open, inode equality, 0600 mode, required flock failure, lock order, redacted RMW return/write identity, temp fsync before replace, and parent fsync after replace.

```bash
git add pico/file_lock.py pico/checkpoint_store.py tests/test_file_lock.py tests/test_checkpoint_store_durability.py
git commit -m "feat(recovery): make checkpoint writes durable"
```

---

### Task 3: Strict Enumeration and Private Invalid-Record Quarantine

**Depends on:** Tasks 1 and 2.

**Files:**
- Modify: `pico/checkpoint_store.py`
- Create: `tests/test_checkpoint_store_invalid_records.py`

**Interfaces:**
- Produces: strict/non-strict schema-validating enumeration, opaque invalid identities, hash-CAS quarantine, and quarantine inspection.

```python
CheckpointStore.quarantine_invalid_record(
    opaque_id: str,
    *,
    expected_raw_hash: str,
) -> dict

CheckpointStore.list_quarantined_records() -> list[dict]
```

- [ ] **Step 1: Write failing invalid-record and quarantine tests**

```python
import hashlib
import json
import os
import stat

import pytest

from pico.checkpoint_store import CheckpointStore


def test_strict_enumeration_rejects_malformed_record(tmp_path):
    store = CheckpointStore(tmp_path)
    path = store.records_dir / "ckpt_broken.json"
    path.write_bytes(b"{broken")

    with pytest.raises(ValueError, match="invalid_record"):
        store.list_checkpoint_records(strict=True)


def test_inspection_returns_invalid_placeholder_without_raw_bytes(tmp_path):
    store = CheckpointStore(tmp_path)
    raw = b"{broken-secret-value"
    secret_filename = "github_pat_secret_filename.json"
    source = store.tool_changes_dir / secret_filename
    source.write_bytes(raw)
    identity_body = b"tool_change\0tool_changes/" + secret_filename.encode() + b"\0" + raw
    opaque_id = "invalid_" + hashlib.sha256(identity_body).hexdigest()

    records = store.list_tool_change_records(strict=False)

    assert records == [{
        "opaque_id": opaque_id,
        "record_kind": "tool_change",
        "status": "invalid_record",
        "raw_hash": hashlib.sha256(raw).hexdigest(),
        "quarantinable": True,
    }]
    assert b"broken-secret-value" not in json.dumps(records).encode()
    assert "github_pat_secret_filename" not in json.dumps(records)


@pytest.mark.parametrize(
    "record",
    [
        {"schema_version": "tool-change-record-v0", "tool_change_id": "tc_requested", "status": "pending"},
        {"schema_version": "tool-change-record-v1", "tool_change_id": "tc_other", "status": "pending"},
        {"schema_version": "tool-change-record-v1", "tool_change_id": "tc_requested", "status": "mystery"},
    ],
)
def test_non_strict_enumeration_hides_schema_id_and_status_invalid_records(tmp_path, record):
    store = CheckpointStore(tmp_path)
    raw = json.dumps(record).encode()
    (store.tool_changes_dir / "tc_requested.json").write_bytes(raw)

    [result] = store.list_tool_change_records(strict=False)

    assert result["status"] == "invalid_record"
    relative = b"tool_changes/tc_requested.json"
    assert result["opaque_id"] == "invalid_" + hashlib.sha256(
        b"tool_change\0" + relative + b"\0" + raw
    ).hexdigest()
    assert "tc_requested" not in json.dumps(result)


def test_quarantine_preserves_raw_bytes_and_private_metadata(tmp_path):
    redactor_calls = []

    def redactor(value):
        redactor_calls.append(value)
        return value

    store = CheckpointStore(tmp_path, redactor=redactor)
    raw = b"{invalid-evidence"
    source = store.tool_changes_dir / "secret-token-filename.json"
    source.write_bytes(raw)
    [preview] = store.list_tool_change_records(strict=False)

    result = store.quarantine_invalid_record(
        preview["opaque_id"],
        expected_raw_hash=preview["raw_hash"],
    )

    raw_path = store.root / result["quarantine_raw_path"]
    metadata_path = store.root / result["quarantine_metadata_path"]
    assert raw_path.read_bytes() == raw
    assert result["raw_hash"] == hashlib.sha256(raw).hexdigest()
    assert stat.S_IMODE(raw_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o600
    assert not source.exists()
    inspected = store.list_quarantined_records()[0]
    assert inspected["opaque_id"] == preview["opaque_id"]
    assert inspected["status"] == "quarantined"
    assert "secret-token-filename" not in json.dumps(inspected)
    assert redactor_calls


def test_quarantine_apply_reenumerates_and_rejects_replaced_bytes(tmp_path):
    store = CheckpointStore(tmp_path)
    source = store.records_dir / "secret-filename.json"
    source.write_bytes(b"{first-invalid-bytes")
    [preview] = store.list_checkpoint_records(strict=False)
    source.write_bytes(b"{replacement-invalid-bytes")

    with pytest.raises(ValueError, match="invalid_record_changed"):
        store.quarantine_invalid_record(
            preview["opaque_id"],
            expected_raw_hash=preview["raw_hash"],
        )

    assert source.read_bytes() == b"{replacement-invalid-bytes"
    assert store.list_quarantined_records() == []


def test_identical_invalid_bytes_at_two_paths_have_distinct_resolvable_ids(tmp_path):
    store = CheckpointStore(tmp_path)
    raw = b"{same-invalid-bytes"
    first = store.tool_changes_dir / "first.json"
    second = store.tool_changes_dir / "second.json"
    first.write_bytes(raw)
    second.write_bytes(raw)
    previews = store.list_tool_change_records(strict=False)
    assert len({item["opaque_id"] for item in previews}) == 2
    for preview in previews:
        store.quarantine_invalid_record(
            preview["opaque_id"], expected_raw_hash=preview["raw_hash"]
        )
    assert not first.exists() and not second.exists()
    assert len(store.list_quarantined_records()) == 2


def test_record_listing_never_follows_leaf_symlink_or_opens_fifo(tmp_path):
    store = CheckpointStore(tmp_path)
    outside = tmp_path / "outside-invalid"
    outside.write_bytes(b"outside-secret-evidence")
    (store.records_dir / "linked.json").symlink_to(outside)
    with pytest.raises(ValueError, match="invalid_record"):
        store.list_checkpoint_records(strict=True)
    inspected = store.list_checkpoint_records(strict=False)
    assert "outside-secret-evidence" not in json.dumps(inspected)

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable")
    (store.records_dir / "linked.json").unlink()
    os.mkfifo(store.records_dir / "blocked.json", 0o600)
    with pytest.raises(ValueError, match="invalid_record"):
        store.list_checkpoint_records(strict=True)


def test_non_regular_records_are_explicitly_quarantinable_without_following(tmp_path):
    store = CheckpointStore(tmp_path)
    outside = tmp_path / "outside-evidence"
    outside.write_bytes(b"must-not-be-read")
    linked = store.records_dir / "linked.json"
    linked.symlink_to(outside)

    [link_preview] = store.list_checkpoint_records(strict=False)
    assert link_preview["quarantinable"] is True
    link_result = store.quarantine_invalid_record(
        link_preview["opaque_id"], expected_raw_hash=link_preview["raw_hash"]
    )
    quarantined_link = store.root / link_result["quarantine_evidence_path"]
    assert not os.path.lexists(linked)
    assert quarantined_link.is_symlink()
    assert os.readlink(quarantined_link) == str(outside)
    assert outside.read_bytes() == b"must-not-be-read"

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable")
    fifo = store.tool_changes_dir / "blocked.json"
    os.mkfifo(fifo, 0o600)
    [fifo_preview] = store.list_tool_change_records(strict=False)
    fifo_result = store.quarantine_invalid_record(
        fifo_preview["opaque_id"], expected_raw_hash=fifo_preview["raw_hash"]
    )
    quarantined_fifo = store.root / fifo_result["quarantine_evidence_path"]
    assert not os.path.lexists(fifo)
    assert stat.S_ISFIFO(quarantined_fifo.lstat().st_mode)
    assert len(store.list_quarantined_records()) == 2


def test_regular_check_to_open_fifo_swap_is_nonblocking(tmp_path, monkeypatch):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable")
    import pico.checkpoint_store as checkpoint_store_module

    store = CheckpointStore(tmp_path)
    source = store.records_dir / "raced.json"
    source.write_bytes(b"{invalid")
    real_open = os.open

    def racing_open(path, flags, *args, **kwargs):
        if os.fspath(path) == os.fspath(source):
            assert flags & getattr(os, "O_NONBLOCK", 0)
            source.unlink()
            os.mkfifo(source, 0o600)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(checkpoint_store_module.os, "open", racing_open)

    [preview] = store.list_checkpoint_records(strict=False)

    assert preview["status"] == "invalid_record"
    assert preview["quarantinable"] is True
```

- [ ] **Step 2: Run the tests and confirm RED**

```bash
./.venv/bin/python -m pytest tests/test_checkpoint_store_invalid_records.py -q
```

Expected: current listing either skips malformed/schema-invalid/internal-ID-invalid/status-invalid input or exposes raw stem/path, and hash-CAS opaque quarantine does not exist.

- [ ] **Step 3: Implement strict listing and exact-byte quarantine**

Add the standard-library `base64` import. Use this stable opaque identity and invalid placeholder; neither accepts nor emits a raw stem/path:

```python
def _opaque_invalid_id(record_kind, relative_path, raw):
    identity = (
        record_kind.encode("ascii")
        + b"\0"
        + relative_path.as_posix().encode("utf-8")
        + b"\0"
        + raw
    )
    return "invalid_" + hashlib.sha256(identity).hexdigest()


def _invalid_placeholder(record_kind, relative_path, raw):
    raw_hash = hashlib.sha256(raw).hexdigest()
    return {
        "opaque_id": _opaque_invalid_id(record_kind, relative_path, raw),
        "record_kind": record_kind,
        "status": "invalid_record",
        "raw_hash": raw_hash,
        "quarantinable": True,
    }


def _non_regular_lstat_evidence(path):
    before = path.lstat()
    payload = {
        "file_type": stat.S_IFMT(before.st_mode),
        "mode": before.st_mode,
        "device": before.st_dev,
        "inode": before.st_ino,
        "uid": before.st_uid,
        "gid": before.st_gid,
        "size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
        "ctime_ns": before.st_ctime_ns,
        "link_target_b64": "",
    }
    if stat.S_ISLNK(before.st_mode):
        payload["link_target_b64"] = base64.b64encode(
            os.readlink(os.fsencode(path))
        ).decode("ascii")
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_mode) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
    ):
        raise CheckpointStoreError("record_changed", "record inode changed")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_regular_record_bytes_no_follow(path):
    require_regular_no_symlink(path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if not stat.S_ISREG(opened.st_mode):
            raise CheckpointStoreError("invalid_record_type", "record is not regular")
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise CheckpointStoreError("record_changed", "record inode changed")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_record_for_listing(path, record_kind, strict, store_root):
    relative_path = path.relative_to(store_root)
    try:
        raw = _read_regular_record_bytes_no_follow(path)
    except (OSError, ValueError, CheckpointStoreError) as exc:
        if strict:
            raise CheckpointStoreError("invalid_record", "invalid record") from exc
        evidence = _non_regular_lstat_evidence(path)
        return _invalid_placeholder(record_kind, relative_path, evidence)
    try:
        record = _with_additive_defaults(json.loads(raw.decode("utf-8")), kind=record_kind)
        if record_kind == "checkpoint":
            return _validate_checkpoint_record(record, expected_id=path.stem)
        return _validate_tool_change_record(record, expected_id=path.stem)
    except (UnicodeDecodeError, json.JSONDecodeError, CheckpointStoreError) as exc:
        if strict:
            raise CheckpointStoreError("invalid_record", "invalid record") from exc
        return _invalid_placeholder(record_kind, relative_path, raw)
```

`_non_regular_lstat_evidence(path)` must use `lstat()` only. It serializes a deterministic private evidence token from file type, mode, device/inode, uid/gid, size, and nanosecond mtime/ctime. For a symlink it additionally obtains the link text with `os.readlink(os.fsencode(path))`, which returns bytes on POSIX; it never opens or stats the target. The token is used only to derive `raw_hash`/opaque ID and as private quarantine evidence, and neither the token nor the link text is returned by inspection. Because this gives non-regular records a stable opaque/hash-CAS identity, their placeholders also set `quarantinable=True`.

For `quarantine_invalid_record(opaque_id, expected_raw_hash=preview["raw_hash"])`, acquire mutation lock then store lock, re-enumerate both active record directories with private unlocked scanners, and require an exact opaque/hash match. A missing old opaque after re-enumeration is `invalid_record_changed`; do not quarantine a replacement inode. A regular invalid record is moved to `quarantine/<record_kind>/<opaque_id>.raw` exactly as before. A symlink/FIFO/socket/device is never opened, followed, or chmodded: recompute its `lstat`/`readlink` evidence under the locks, verify the hash and `(st_dev, st_ino, st_mode)` CAS tuple, then atomically `os.rename()` that exact inode to `quarantine/<record_kind>/<opaque_id>.inode`; verify the destination with `lstat()` and fsync both parents. The renamed symlink itself preserves exact link evidence without dereferencing it; other special inodes preserve their lstat identity outside the active record directories. If rename or the post-rename inode check fails, raise a stable error and retain whichever private evidence location exists for manual recovery—never reopen it. This explicit apply path removes the active strict-enumeration blocker, so a non-regular record cannot become a permanent write DoS.

Write redacted metadata with `opaque_id`, `record_kind`, `status="quarantined"`, `evidence_kind` (`raw` or `inode`), `raw_hash`, `quarantined_at`, `quarantine_metadata_path`, and exactly one of `quarantine_raw_path`/`quarantine_evidence_path`, all paths derived only from `<record_kind>/<opaque_id>`. Metadata contains no original stem/path or link target. Regular raw bytes and special-inode evidence bypass redaction and remain exact/private. `list_quarantined_records()` validates metadata but never opens, stats, or resolves either evidence path.

- [ ] **Step 4: Run focused and existing store tests**

```bash
./.venv/bin/python -m pytest tests/test_checkpoint_store_invalid_records.py tests/test_checkpoint_store_security.py tests/test_checkpoint_store_phase1.py -q
./.venv/bin/python -m ruff check pico/checkpoint_store.py tests/test_checkpoint_store_invalid_records.py
git diff --check
```

Expected: all commands exit 0; invalid raw bytes and secret filenames never appear in placeholders/metadata/output, replacement bytes fail CAS, and quarantined raw evidence remains exact/private.

- [ ] **Step 5: Review and commit Task 3**

Review full schema/type/status/internal-ID validation, opaque ID derivation from internal store-relative path + raw/evidence bytes + kind, duplicate-byte uniqueness, absence of raw stem/path/link target in output, no-follow and nonblocking regular reads, pre-open FIFO-swap rejection, preview/apply re-enumeration, raw-hash plus inode CAS, safe special-inode rename out of active directories, mutation→store lock order, metadata redaction, exact raw/evidence preservation, and inspection that never touches evidence paths.

```bash
git add pico/checkpoint_store.py tests/test_checkpoint_store_invalid_records.py
git commit -m "feat(recovery): quarantine invalid checkpoint records"
```

---

### Task 4: Complete FileEntry Capture from the Same Eligible Bytes

**Depends on:** Task 1 and A1 security interfaces.

**Files:**
- Modify: `pico/recovery_policy.py`
- Modify: `pico/tool_executor.py`
- Modify: `tests/test_recovery_policy.py`
- Modify: `tests/test_tool_executor.py`

**Interfaces:**
- Produces: `snapshot_bytes_eligibility(workspace_root, raw_path, data, *, max_blob_size) -> dict` and the complete FileEntry contract consumed by Tasks 7 through 10.

- [ ] **Step 1: Write failing byte-eligibility and FileEntry tests**

Add these focused assertions to the existing test modules:

```python
def test_snapshot_bytes_eligibility_blocks_sensitive_content(tmp_path, monkeypatch):
    from pico.recovery_policy import snapshot_bytes_eligibility

    monkeypatch.setenv("PICO_OPENAI_API_KEY", "sk-sensitive-recovery-value")
    result = snapshot_bytes_eligibility(
        tmp_path,
        "src/config.py",
        b'KEY = "sk-sensitive-recovery-value"\n',
        max_blob_size=1024,
    )
    assert result["snapshot_eligible"] is False
    assert result["ineligible_reason"] == "sensitive_content"


@pytest.mark.parametrize(
    "data",
    (
        b"text-prefix\x00text-suffix",
        bytes([1, 2, 3, 4, 5, 6, 7, 8]) * 64,
    ),
)
def test_snapshot_bytes_eligibility_preserves_binary_detection(tmp_path, data):
    result = snapshot_bytes_eligibility(
        tmp_path, "artifact.dat", data, max_blob_size=4096
    )
    assert result["snapshot_eligible"] is False
    assert result["ineligible_reason"] == "binary_file"


def test_write_file_entry_records_exists_hash_and_mode(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o640)
    agent = build_agent(tmp_path)

    result = agent.run_tool("write_file", {"path": "note.txt", "content": "after"})
    entry = result.metadata["file_entries"][0]

    assert entry["before_exists"] is True
    assert entry["after_exists"] is True
    assert entry["before_blob_ref"] == entry["before_hash"]
    assert entry["after_blob_ref"] == entry["after_hash"]
    assert entry["expected_current_hash"] == entry["after_hash"]
    assert entry["before_mode"] == 0o640
    assert entry["after_mode"] == 0o640
    assert entry["source_tool_change_ids"] == [result.metadata["tool_change_id"]]


def test_sensitive_bytes_never_reach_blob_store(tmp_path, monkeypatch):
    sentinel = "sk-sensitive-recovery-value"
    monkeypatch.setenv("PICO_OPENAI_API_KEY", sentinel)
    target = tmp_path / "safe.py"
    target.write_text("before", encoding="utf-8")
    agent = build_agent(tmp_path)

    result = agent.run_tool("write_file", {"path": "safe.py", "content": sentinel})

    assert result.metadata["tool_status"] == "rejected"
    for path in agent.checkpoint_store.blobs_dir.rglob("*"):
        if path.is_file():
            assert sentinel.encode() not in path.read_bytes()
```

- [ ] **Step 2: Run the tests and confirm RED**

```bash
./.venv/bin/python -m pytest \
  tests/test_recovery_policy.py::test_snapshot_bytes_eligibility_blocks_sensitive_content \
  tests/test_recovery_policy.py::test_snapshot_bytes_eligibility_preserves_binary_detection \
  tests/test_tool_executor.py::test_write_file_entry_records_exists_hash_and_mode \
  tests/test_tool_executor.py::test_sensitive_bytes_never_reach_blob_store -q
```

Expected: `snapshot_bytes_eligibility` is missing and FileEntry lacks existence/mode fields.

- [ ] **Step 3: Implement exact-byte eligibility and complete FileEntry fields**

Add this byte owner in `pico/recovery_policy.py`; `snapshot_eligibility()` must read once and delegate to it:

```python
def snapshot_bytes_eligibility(workspace_root, raw_path, data, *, max_blob_size):
    normalized = normalize_workspace_relative_path(raw_path)
    if is_sensitive_path(normalized):
        return {"snapshot_eligible": False, "ineligible_reason": "sensitive_path", "path": normalized}
    if len(data) > max_blob_size:
        return {"snapshot_eligible": False, "ineligible_reason": "file_too_large", "path": normalized}
    if Path(normalized).suffix.casefold() in _BINARY_EXTENSIONS or _looks_binary(data[:4096]):
        return {"snapshot_eligible": False, "ineligible_reason": "binary_file", "path": normalized}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"snapshot_eligible": False, "ineligible_reason": "binary_file", "path": normalized}
    if contains_secret_material(text):
        return {"snapshot_eligible": False, "ineligible_reason": "sensitive_content", "path": normalized}
    return {"snapshot_eligible": True, "ineligible_reason": "", "path": normalized}
```

In `_build_file_entries()`, calculate `before_exists/after_exists` explicitly, use `stat.S_IMODE(path.stat().st_mode)` for present states, and set absent hash/ref to the empty string and absent mode to `None`. Only pass the bytes already approved by `snapshot_bytes_eligibility()` to `write_blob()`. After Tool Change start assigns its ID, every captured entry receives `source_tool_change_ids=[tool_change_id]`; no later writer infers provenance from list position.

- [ ] **Step 4: Run focused and ToolExecutor regression tests**

```bash
./.venv/bin/python -m pytest tests/test_recovery_policy.py tests/test_tool_executor.py -q
./.venv/bin/python -m ruff check pico/recovery_policy.py pico/tool_executor.py tests/test_recovery_policy.py tests/test_tool_executor.py
git diff --check
```

Expected: all commands exit 0; every present state has a valid hash and normalized mode.

- [ ] **Step 5: Review and commit Task 4**

Review that eligibility and blob write use one byte value, extension/NUL/control-byte binary detection remains intact before UTF-8 decoding, absent states never pretend an unknown hash is proof, and Git HEAD fallback uses the same byte policy.

```bash
git add pico/recovery_policy.py pico/tool_executor.py tests/test_recovery_policy.py tests/test_tool_executor.py
git commit -m "feat(recovery): capture complete file states"
```

---

### Task 5: Owner-Safe Tool Change Lifecycle and Pending Review

**Depends on:** Tasks 2 and 3.

**Files:**
- Modify: `pico/recovery_models.py`
- Modify: `pico/tool_change_recorder.py`
- Modify: `tests/test_recovery_models.py`
- Modify: `tests/test_tool_change_recorder.py`

**Interfaces:**
- Produces: prepared Tool Change start, owner/status CAS finalize, all-owner pending enumeration, and explicit pending resolution.

```python
ToolChangeRecorder.start(
    checkpoint_id,
    turn_id,
    tool_name,
    effect_class,
    input_summary,
    *,
    prepared_file_entries=None,
    recovery_context=None,
) -> dict

ToolChangeRecorder.pending_recovery_reviews() -> list[dict]

ToolChangeRecorder.resolve_pending(
    tool_change_id,
    *,
    reviewed_by,
    review_reason,
) -> dict
```

- [ ] **Step 1: Write failing owner and transition tests**

```python
import pytest

from pico.checkpoint_store import CheckpointStore
from pico.tool_change_recorder import ToolChangeRecorder


def test_finalize_requires_matching_owner_and_pending_status(tmp_path):
    store = CheckpointStore(tmp_path)
    owner = ToolChangeRecorder(store, owner_id="owner-a")
    foreign = ToolChangeRecorder(store, owner_id="owner-b")
    pending = owner.start("", "turn-1", "write_file", "workspace_write", {})

    with pytest.raises(ValueError, match="owner_mismatch"):
        foreign.finalize(pending["tool_change_id"], "finalized")

    owner.finalize(pending["tool_change_id"], "finalized")
    with pytest.raises(ValueError, match="status_conflict"):
        owner.finalize(pending["tool_change_id"], "error")


def test_pending_review_does_not_exclude_same_owner(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store, owner_id="owner-a")
    current = recorder.start("", "turn-1", "memory_save", "memory_write", {})
    foreign = ToolChangeRecorder(store, owner_id="owner-b").start(
        "", "turn-2", "write_file", "workspace_write", {}
    )

    ids = {item["tool_change_id"] for item in recorder.pending_recovery_reviews()}
    assert ids == {current["tool_change_id"], foreign["tool_change_id"]}


def test_start_persists_prepared_state_and_recovery_context(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store, owner_id="owner-a")
    prepared = [{"path": "note.txt", "before_exists": False}]
    context = {"observer_mode": "filesystem", "git_head": "abc123"}

    record = recorder.start(
        "",
        "turn-1",
        "write_file",
        "workspace_write",
        {"path": "note.txt"},
        prepared_file_entries=prepared,
        recovery_context=context,
    )

    assert record["prepared_file_entries"] == prepared
    assert record["recovery_context"] == context


def test_resolve_pending_is_reviewed_interrupted_transition(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store, owner_id="owner-a")
    pending = recorder.start("", "turn-1", "write_file", "workspace_write", {})

    resolved = recorder.resolve_pending(
        pending["tool_change_id"],
        reviewed_by="cli",
        review_reason="explicit_cli_resolution",
    )

    assert resolved["status"] == "interrupted"
    assert resolved["reviewed_by"] == "cli"
    assert resolved["review_reason"] == "explicit_cli_resolution"
    assert resolved["reviewed_at"]
```

- [ ] **Step 2: Run tests and confirm RED**

```bash
./.venv/bin/python -m pytest tests/test_recovery_models.py tests/test_tool_change_recorder.py -q
```

Expected: foreign finalize succeeds, terminal records can be overwritten, prepared fields/interfaces are missing, or same-owner pending is excluded.

- [ ] **Step 3: Implement additive fields and CAS transitions**

Use the prepared/recovery/review defaults introduced in Task 1; Task 5 changes lifecycle behavior, not their names or container types.

Use store RMW for finalize:

```python
def finalize(self, tool_change_id, status, **fields):
    if status not in _ALLOWED_TERMINAL_STATUSES:
        raise ValueError("unsupported terminal status: " + str(status))

    def transform(record):
        if record.get("owner_id", "") != self.owner_id:
            raise ValueError("owner_mismatch")
        if record.get("status") != "pending":
            raise ValueError("status_conflict")
        record["status"] = status
        record["ended_at"] = utc_now()
        for key, value in fields.items():
            if value is not None:
                record[key] = value
        return record

    return self.store.update_tool_change_record(
        tool_change_id,
        transform,
        expected_status="pending",
    )
```

`pending_recovery_reviews()` must call `list_tool_change_records(strict=True)` and return every record whose status is `pending`, without owner filtering. `resolve_pending()` must acquire `store.mutation_lock()`, then perform a pending-status RMW that writes `interrupted`, `ended_at`, and reviewed metadata.

- [ ] **Step 4: Run focused lifecycle regressions**

```bash
./.venv/bin/python -m pytest tests/test_recovery_models.py tests/test_tool_change_recorder.py -q
./.venv/bin/python -m ruff check pico/recovery_models.py pico/tool_change_recorder.py tests/test_recovery_models.py tests/test_tool_change_recorder.py
git diff --check
```

Expected: all commands exit 0 and no terminal record is mutable through `finalize()` or `resolve_pending()`.

- [ ] **Step 5: Review and commit Task 5**

Review that no startup path calls `mark_interrupted_pending()`, every transition is one store-lock RMW, and owner validation happens inside that RMW.

```bash
git add pico/recovery_models.py pico/tool_change_recorder.py tests/test_recovery_models.py tests/test_tool_change_recorder.py
git commit -m "feat(recovery): enforce tool change ownership"
```

---

### Task 6: Serialize the Complete Workspace and Memory Mutation Lifecycle

**Depends on:** Tasks 2 through 5.

**Files:**
- Modify: `pico/tool_executor.py`
- Modify: `pico/runtime.py`
- Modify: `pico/recovery_manager.py`
- Create: `tests/test_tool_executor_mutation_lock.py`
- Modify: `tests/test_tool_executor.py`
- Modify: `tests/test_recovery_manager.py`

**Interfaces:**
- Consumes: `CheckpointStore.mutation_lock()`, strict listing, complete FileEntry capture, and owner-safe Tool Change methods.
- Produces: rejection metadata `tool_error_code="recovery_review_required"` before every blocked runner.
- Produces early: `RecoveryManager.pending_restore_reviews() -> list[dict]`, strict-listing all applying and unreviewed partial journals; Task 10 reuses it.

- [ ] **Step 1: Write failing mutation-order and guard tests**

```python
from contextlib import contextmanager

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext


def build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )


def test_approval_finishes_before_mutation_lock(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    events = []
    monkeypatch.setattr(agent, "approve", lambda name, args: events.append("approval") or True)

    @contextmanager
    def lock():
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    monkeypatch.setattr(agent.checkpoint_store, "mutation_lock", lock)
    agent.run_tool("write_file", {"path": "note.txt", "content": "value"})

    assert events.index("approval") < events.index("lock-enter")


def test_existing_same_owner_pending_blocks_runner(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    agent.tool_change_recorder.start(
        "",
        "old-turn",
        "write_file",
        "workspace_write",
        {"path": "old.txt"},
    )
    calls = []
    monkeypatch.setitem(agent.tools["write_file"], "run", lambda args: calls.append(args) or "ok")

    result = agent.run_tool("write_file", {"path": "new.txt", "content": "value"})

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "recovery_review_required"
    assert calls == []


def test_malformed_mutation_record_blocks_runner_via_strict_guard(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    (agent.checkpoint_store.tool_changes_dir / "secret-filename.json").write_bytes(b"{invalid")
    calls = []
    monkeypatch.setitem(agent.tools["write_file"], "run", lambda args: calls.append(args) or "ok")

    result = agent.run_tool("write_file", {"path": "new.txt", "content": "value"})

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "recovery_review_required"
    assert calls == []


def test_memory_write_uses_same_mutation_lock(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    events = []

    @contextmanager
    def lock():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    monkeypatch.setattr(agent.checkpoint_store, "mutation_lock", lock)
    agent.run_tool("memory_save", {"topic": "decision", "content": "safe local note"})
    assert events == ["enter", "exit"]


def test_finalize_failure_blocks_next_same_owner_mutation(tmp_path, monkeypatch):
    agent = build_agent(tmp_path)
    real_finalize = agent.tool_change_recorder.finalize
    calls = {"count": 0}

    def fail_once(tool_change_id, status, **fields):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("simulated finalize failure")
        return real_finalize(tool_change_id, status, **fields)

    monkeypatch.setattr(agent.tool_change_recorder, "finalize", fail_once)
    first = agent.run_tool("write_file", {"path": "first.txt", "content": "first"})
    second = agent.run_tool("write_file", {"path": "second.txt", "content": "second"})

    assert first.metadata["tool_status"] == "error"
    assert second.metadata["tool_error_code"] == "recovery_review_required"
    assert not (tmp_path / "second.txt").exists()
```

- [ ] **Step 2: Run the focused tests and confirm RED**

```bash
./.venv/bin/python -m pytest tests/test_tool_executor_mutation_lock.py -q
```

Expected: mutation lock is absent from the lifecycle, same-owner pending does not block, or memory mutation executes outside the lock.

- [ ] **Step 3: Route both mutation effect classes through one gate**

Refactor only the mutation branch of `ToolExecutor.execute()` to this order:

```python
if effect_class in {"workspace_write", "memory_write"}:
    with agent.checkpoint_store.mutation_lock():
        reviews = agent.tool_change_recorder.pending_recovery_reviews()
        restore_reviews = agent.recovery_manager.pending_restore_reviews()
        if reviews or restore_reviews:
            return ToolExecutionResult(
                content="error: recovery review required",
                metadata=_metadata(
                    "rejected",
                    effect_class=effect_class,
                    tool_error_code="recovery_review_required",
                    security_event_type="recovery_review_required",
                    risk_level="high",
                ),
            )
        return self._execute_mutation_locked(name, args, tool, effect_class)
```

Move the current before-capture, `start()`, runner, after-capture, and `finalize()` body into `_execute_mutation_locked()`. Capture prepared direct-file state or shell recovery context before calling `start()`, and pass it into the start record. Approval and argument validation remain before the lock. If finalize fails, return stable error metadata and leave the pending record for the next guard. Guard methods use strict store enumeration and convert any malformed record into `recovery_review_required`; they never call Task 11's non-strict `collect_recovery_review_items()` inspection helper.

Implement `RecoveryManager.pending_restore_reviews()` in this task using `list_checkpoint_records(strict=True)`. It returns every restore checkpoint with `status="applying"` and every `status="partial"` record whose `reviewed_at` is empty, regardless of owner. It returns safe validated summaries only and never writes.

- [ ] **Step 4: Run focused and existing ToolExecutor tests**

```bash
./.venv/bin/python -m pytest tests/test_tool_executor_mutation_lock.py tests/test_tool_executor.py tests/test_tool_change_recorder.py tests/test_recovery_manager.py -q
./.venv/bin/python -m ruff check pico/tool_executor.py pico/runtime.py pico/recovery_manager.py tests/test_tool_executor_mutation_lock.py tests/test_tool_executor.py tests/test_recovery_manager.py
git diff --check
```

Expected: all commands exit 0; every rejected mutation has runner count zero.

- [ ] **Step 5: Review and commit Task 6**

Review the exact event order, ensure read-only tools do not take the mutation lock, and verify no pending guard excludes the current owner.

```bash
git add pico/tool_executor.py pico/runtime.py pico/recovery_manager.py tests/test_tool_executor_mutation_lock.py tests/test_tool_executor.py tests/test_recovery_manager.py
git commit -m "feat(recovery): serialize mutation tool lifecycle"
```

---

### Task 7: Coalesce One Net FileEntry per Path

**Depends on:** Tasks 4 and 5.

**Files:**
- Modify: `pico/recovery_models.py`
- Modify: `pico/recovery_checkpoint_writer.py`
- Modify: `tests/test_recovery_checkpoint_writer.py`

**Interfaces:**
- Produces strict FileEntry validation, legacy downgrade reasons, coalescing, and checkpoint `integrity_errors`.

```python
validate_file_entry(entry: dict, *, legacy: bool = False) -> str
coalesce_file_entries(entries: list[dict], *, legacy: bool = False) -> list[dict]
```

- [ ] **Step 1: Write failing coalescing and continuity tests**

```python
import pytest

from pico.checkpoint_store import CheckpointStore
from pico.recovery_checkpoint_writer import (
    RecoveryCheckpointWriter,
    coalesce_file_entries,
    validate_file_entry,
)
from pico.tool_change_recorder import ToolChangeRecorder


def _state_entry(path, before, after, source_id):
    before_exists, before_hash, before_mode = before
    after_exists, after_hash, after_mode = after
    return {
        "path": path,
        "change_kind": "modified",
        "snapshot_eligible": True,
        "ineligible_reason": "",
        "before_exists": before_exists,
        "before_blob_ref": before_hash if before_exists else "",
        "before_hash": before_hash,
        "before_mode": before_mode,
        "after_exists": after_exists,
        "after_blob_ref": after_hash if after_exists else "",
        "after_hash": after_hash,
        "after_mode": after_mode,
        "expected_current_hash": after_hash,
        "source_tool_change_ids": [source_id],
    }


def test_a_b_c_coalesces_to_a_c():
    a = "a" * 64
    b = "b" * 64
    c = "c" * 64
    entries = [
        _state_entry("note.txt", (True, a, 0o644), (True, b, 0o644), "tc_1"),
        _state_entry("note.txt", (True, b, 0o644), (True, c, 0o600), "tc_2"),
    ]

    result = coalesce_file_entries(entries)

    assert len(result) == 1
    assert result[0]["before_hash"] == a
    assert result[0]["after_hash"] == c
    assert result[0]["after_mode"] == 0o600
    assert result[0]["source_tool_change_ids"] == ["tc_1", "tc_2"]


def test_present_blank_hash_cannot_prove_continuity():
    c = "c" * 64
    entries = [
        _state_entry("note.txt", (True, "", 0o644), (True, "", 0o644), "tc_1"),
        _state_entry("note.txt", (True, "", 0o644), (True, c, 0o644), "tc_2"),
    ]

    result = coalesce_file_entries(entries)

    assert result[0]["snapshot_eligible"] is False
    assert result[0]["ineligible_reason"] == "discontinuous_history"


def test_same_hash_different_mode_is_not_noop():
    value = "d" * 64
    result = coalesce_file_entries([
        _state_entry("run.sh", (True, value, 0o644), (True, value, 0o755), "tc_1")
    ])
    assert len(result) == 1
    assert result[0]["change_kind"] == "modified"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda entry: entry.update(before_blob_ref="f" * 64), "blob_ref_hash_mismatch"),
        (lambda entry: entry.update(expected_current_hash="f" * 64), "expected_after_hash_mismatch"),
        (lambda entry: entry.update(change_kind="created"), "change_kind_exists_mismatch"),
    ],
)
def test_file_entry_rejects_blob_expected_hash_and_kind_mismatch(mutate, reason):
    value = "d" * 64
    entry = _state_entry("note.txt", (True, value, 0o644), (True, value, 0o600), "tc_1")
    mutate(entry)
    assert validate_file_entry(entry) == reason


def test_legacy_missing_mode_and_ambiguous_sources_are_review_only():
    before = "a" * 64
    after = "b" * 64
    entry = _state_entry("note.txt", (True, before, 0o644), (True, after, 0o644), "tc_1")
    entry.pop("before_mode")
    assert validate_file_entry(entry, legacy=True) == "legacy_mode_unknown"

    complete = _state_entry("note.txt", (True, before, 0o644), (True, after, 0o644), "tc_1")
    complete["source_tool_change_ids"] = []
    [merged] = coalesce_file_entries([complete], legacy=True)
    assert merged["snapshot_eligible"] is False
    assert merged["ineligible_reason"] == "legacy_ambiguous_history"


def test_turn_checkpoint_rejects_pending_or_internal_id_invalid_source(tmp_path):
    store = CheckpointStore(tmp_path)
    recorder = ToolChangeRecorder(store, owner_id="owner")
    pending = recorder.start("", "turn", "write_file", "workspace_write", {})
    writer = RecoveryCheckpointWriter(store, tmp_path)

    checkpoint = writer.create_turn_checkpoint(
        session_id="session",
        run_id="run",
        turn_id="turn",
        parent_checkpoint_id="",
        tool_change_ids=[pending["tool_change_id"]],
    )

    assert checkpoint["integrity_errors"] == [{
        "reason": "incomplete_tool_change_history",
        "tool_change_ids": [pending["tool_change_id"]],
    }]
```

Also replace the existing missing-Tool-Change expectation with:

```python
assert checkpoint["integrity_errors"] == [
    {"reason": "incomplete_tool_change_history", "tool_change_ids": ["tc_missing"]}
]
```

- [ ] **Step 2: Run the tests and confirm RED**

```bash
./.venv/bin/python -m pytest tests/test_recovery_checkpoint_writer.py -q
```

Expected: import of `coalesce_file_entries` fails or the writer emits multiple entries and leaves the checkpoint usable after missing history.

- [ ] **Step 3: Implement deterministic validation and net-state reduction**

Use Task 1's existing `integrity_errors=[]` field. Before coalescing, `validate_file_entry()` enforces:

- `path` is normalized and nonempty; `source_tool_change_ids` is a list of safe IDs for new entries.
- When a state has both hash/ref, each is lowercase SHA-256 and `blob_ref == hash`; an eligible present state requires both.
- `expected_current_hash == after_hash` for a present after-state and is empty for an absent after-state.
- `created` is absent→present, `deleted` is present→absent, and `modified` is present→present.
- New entries require normalized integer modes for present states and `None` for absent states.
- Legacy missing mode returns `legacy_mode_unknown`; legacy entries lacking sufficient ordered source/continuity evidence return `legacy_ambiguous_history`. Neither legacy reason is auto-restorable.

Implement `coalesce_file_entries()` as an order-preserving group by normalized path. For each group:

```python
def _continuous(left, right):
    left_state = (left["after_exists"], left["after_hash"], left["after_mode"])
    right_state = (right["before_exists"], right["before_hash"], right["before_mode"])
    return left_state == right_state


def _valid_present_state(exists, content_hash, mode):
    if not exists:
        return content_hash == "" and mode is None
    return (
        isinstance(content_hash, str)
        and len(content_hash) == 64
        and all(char in "0123456789abcdef" for char in content_hash)
        and isinstance(mode, int)
    )
```

The merged entry takes the first before-state, last after-state, and ordered unique source IDs. Preserve exact legacy reasons; any new invalid/ineligible/discontinuous source produces one review entry with `snapshot_eligible=False` and `ineligible_reason="discontinuous_history"`. Drop only absent→absent or present→present with identical hash and mode. Derive `created/deleted/modified` from net states.

`create_turn_checkpoint()` strict-loads each requested Tool Change, requires `record["tool_change_id"]` to equal the requested ID, and requires a terminal status from `finalized/error/partial_success/interrupted`. Missing, pending, malformed, schema-invalid, or internal-ID-invalid sources contribute their requested safe IDs to one `incomplete_tool_change_history` integrity error; no entry from them is merged.

- [ ] **Step 4: Run focused writer and model tests**

```bash
./.venv/bin/python -m pytest tests/test_recovery_checkpoint_writer.py tests/test_recovery_models.py -q
./.venv/bin/python -m ruff check pico/recovery_models.py pico/recovery_checkpoint_writer.py tests/test_recovery_checkpoint_writer.py
git diff --check
```

Expected: all commands exit 0 and every path appears at most once in a turn checkpoint.

- [ ] **Step 5: Review and commit Task 7**

Review blob/hash equality, expected/after equality, kind/exists transitions, source Tool Change terminal/internal identity, created→modified, modified→deleted, create→delete, content-returned-to-original, mode-only, ineligible-middle, `legacy_ambiguous_history`, `legacy_mode_unknown`, and missing-history cases.

```bash
git add pico/recovery_models.py pico/recovery_checkpoint_writer.py tests/test_recovery_checkpoint_writer.py
git commit -m "feat(recovery): coalesce checkpoint file history"
```

---

### Task 8: Build a Strict Restore Plan Before Any Mutation

**Depends on:** Tasks 1, 3, and 7.

**Files:**
- Modify: `pico/recovery_paths.py`
- Modify: `pico/recovery_manager.py`
- Modify: `tests/test_recovery_paths.py`
- Modify: `tests/test_recovery_manager.py`

**Interfaces:**
- Produces: lexical no-symlink resolution and `preview_restore()` with stable plan status.

```python
resolve_workspace_relative_path_no_symlinks(
    workspace_root,
    raw_path,
    *,
    allow_missing_leaf=True,
) -> Path

RecoveryManager.preview_restore(checkpoint_id: str) -> dict
```

- [ ] **Step 1: Write failing workspace, blob, and plan-precedence tests**

```python
import os

import pytest

from pico.checkpoint_store import CheckpointStore
from pico.recovery_manager import RecoveryManager
from pico.recovery_models import new_checkpoint_record, new_tool_change_record


def test_workspace_mismatch_is_invalid(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record("ckpt_wrong", "turn", "", "", "", "", str(other))
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore("ckpt_wrong")

    assert plan["status"] == "invalid"
    assert plan["entries"][0]["reason"] == "workspace_mismatch"


def test_parent_symlink_is_invalid(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, tmp_path / "linked")
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record("ckpt_link", "turn", "", "", "", "", str(tmp_path))
    record["file_entries"] = [{
        "path": "linked/note.txt",
        "snapshot_eligible": True,
        "before_exists": False,
        "before_blob_ref": "",
        "before_hash": "",
        "before_mode": None,
        "after_exists": False,
        "after_blob_ref": "",
        "after_hash": "",
        "after_mode": None,
        "expected_current_hash": "",
        "change_kind": "created",
        "source_tool_change_ids": [],
    }]
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore("ckpt_link")

    assert plan["status"] == "invalid"
    assert plan["entries"][0]["reason"] == "symlink"


def test_plan_status_precedence_is_stable(tmp_path):
    manager = RecoveryManager(CheckpointStore(tmp_path), tmp_path)
    assert manager._plan_status(["restore"]) == "ready"
    assert manager._plan_status([]) == "noop"
    assert manager._plan_status(["review", "restore"]) == "review_required"
    assert manager._plan_status(["conflict", "review"]) == "conflicted"
    assert manager._plan_status(["error", "conflict"]) == "invalid"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda entry: entry.update(before_blob_ref="f" * 64), "blob_ref_hash_mismatch"),
        (lambda entry: entry.update(expected_current_hash="f" * 64), "expected_after_hash_mismatch"),
        (lambda entry: entry.update(change_kind="created"), "change_kind_exists_mismatch"),
    ],
)
def test_plan_revalidates_untrusted_file_entry_consistency(tmp_path, mutation, reason):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"before")
    after = store.write_blob(b"after")
    (tmp_path / "note.txt").write_bytes(b"after")
    record = new_checkpoint_record(
        "ckpt_tampered", "turn", "", "", "", "", str(tmp_path.resolve())
    )
    entry = {
        "path": "note.txt",
        "change_kind": "modified",
        "snapshot_eligible": True,
        "ineligible_reason": "",
        "before_exists": True,
        "before_blob_ref": before["blob_ref"],
        "before_hash": before["content_hash"],
        "before_mode": 0o644,
        "after_exists": True,
        "after_blob_ref": after["blob_ref"],
        "after_hash": after["content_hash"],
        "after_mode": 0o644,
        "expected_current_hash": after["content_hash"],
        "source_tool_change_ids": [],
    }
    mutation(entry)
    record["file_entries"] = [entry]
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore(record["checkpoint_id"])

    assert plan["status"] == "invalid"
    assert plan["entries"][0]["reason"] == reason


def test_unreviewed_partial_and_legacy_missing_mode_are_not_ready(tmp_path):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"before")
    after = store.write_blob(b"after")
    (tmp_path / "note.txt").write_bytes(b"after")
    record = new_checkpoint_record(
        "ckpt_legacy_partial", "restore", "", "", "", "", str(tmp_path.resolve())
    )
    record["status"] = "partial"
    record["file_entries"] = [{
        "path": "note.txt",
        "change_kind": "modified",
        "snapshot_eligible": True,
        "ineligible_reason": "",
        "before_exists": True,
        "before_blob_ref": before["blob_ref"],
        "before_hash": before["content_hash"],
        "after_exists": True,
        "after_blob_ref": after["blob_ref"],
        "after_hash": after["content_hash"],
        "expected_current_hash": after["content_hash"],
        "source_tool_change_ids": [],
    }]
    store.write_checkpoint_record(record)

    plan = RecoveryManager(store, tmp_path).preview_restore(record["checkpoint_id"])

    assert plan["status"] == "review_required"
    assert {entry["reason"] for entry in plan["entries"]} == {
        "legacy_mode_unknown",
        "partial_review_required",
    }
```

- [ ] **Step 2: Run focused path and plan tests and confirm RED**

```bash
./.venv/bin/python -m pytest tests/test_recovery_paths.py tests/test_recovery_manager.py -q
```

Expected: plan lacks `status`/workspace validation, symlink resolution follows an existing link, or `_plan_status` is absent.

- [ ] **Step 3: Implement lexical resolution and complete plan validation**

Add the resolver in `pico/recovery_paths.py`:

```python
def resolve_workspace_relative_path_no_symlinks(workspace_root, raw_path, *, allow_missing_leaf=True):
    normalized = normalize_workspace_relative_path(raw_path)
    root = Path(workspace_root).resolve()
    candidate = root
    parts = Path(normalized).parts
    for index, part in enumerate(parts):
        candidate = candidate / part
        is_leaf = index == len(parts) - 1
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if is_leaf and allow_missing_leaf:
                return candidate
            raise ValueError("missing_parent")
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symlink")
    candidate.relative_to(root)
    return candidate
```

`preview_restore()` must strict-load and validate source schema, requested/internal ID, resolved workspace root, integrity errors, checkpoint type/status, every FileEntry, sensitive policy, blob presence/hash, and current `(exists, hash, mode)`. For turn checkpoints it also strict-loads every `tool_change_id`, requires matching internal ID and a terminal status, and rejects missing/pending/invalid sources as `incomplete_tool_change_history`. It independently enforces `blob_ref == declared hash`, `expected_current_hash == after_hash`, and kind/exists transitions even when a writer produced the record. It coalesces legacy duplicate entries in memory and returns `legacy_ambiguous_history` or `legacy_mode_unknown` rather than guessing. Return `status` using this precedence:

```python
def _plan_status(self, decisions):
    if "error" in decisions:
        return "invalid"
    if "conflict" in decisions:
        return "conflicted"
    if "review" in decisions:
        return "review_required"
    if "restore" in decisions:
        return "ready"
    return "noop"
```

Restore checkpoint source rules are exact: `applied` is eligible; `partial` is eligible only when `reviewed_at` is present and only its proven `file_entries` are considered; unreviewed `partial` is `partial_review_required`; `applying` is `review_required`; `blocked`, `failed`, and `noop` have no auto-restorable mutation set. Preview remains read-only for all statuses.

- [ ] **Step 4: Run focused and recovery-manager regressions**

```bash
./.venv/bin/python -m pytest tests/test_recovery_paths.py tests/test_recovery_manager.py tests/test_recovery_checkpoint_writer.py -q
./.venv/bin/python -m ruff check pico/recovery_paths.py pico/recovery_manager.py tests/test_recovery_paths.py tests/test_recovery_manager.py
git diff --check
```

Expected: all commands exit 0; preview performs no write or workspace mutation.

- [ ] **Step 5: Review and commit Task 8**

Review all stable error reasons, deep FileEntry invariants, source Tool Change terminal/internal identity, legacy downgrade reasons, restore-status source rules, whole-plan blocking, and zero preview writes/mutations.

```bash
git add pico/recovery_paths.py pico/recovery_manager.py tests/test_recovery_paths.py tests/test_recovery_manager.py
git commit -m "feat(recovery): validate restore plans"
```

---

### Task 9: Write Durable Restore Intents Before a Successful Apply

**Depends on:** Tasks 2, 4, and 8.

**Files:**
- Modify: `pico/recovery_models.py`
- Modify: `pico/recovery_checkpoint_writer.py`
- Modify: `pico/recovery_manager.py`
- Create: `tests/test_recovery_journal.py`

**Interfaces:**
- Produces: status/owner-aware restore checkpoint creation and mutation-lock-scoped, strict-plan-rebuilding, journal-first `apply_restore()`.

```python
RecoveryCheckpointWriter.create_restore_checkpoint(
    session_id,
    run_id,
    turn_id,
    parent_checkpoint_id,
    restore_provenance,
    *,
    status,
    owner_id,
    file_entries=None,
    verification_evidence=None,
) -> dict

RecoveryManager.apply_restore(checkpoint_id: str) -> dict
```

- [ ] **Step 1: Write failing journal-order, fsync, and mode tests**

```python
import json
import os
from contextlib import contextmanager

import pytest

from pico.checkpoint_store import CheckpointStore
from pico.recovery_checkpoint_writer import RecoveryCheckpointWriter
from pico.recovery_manager import RecoveryManager
from pico.recovery_models import new_checkpoint_record, new_tool_change_record


def _modified_checkpoint(store, root, *, checkpoint_id="ckpt_source"):
    before = store.write_blob(b"before")
    after = store.write_blob(b"after")
    target = root / "note.txt"
    target.write_bytes(b"after")
    target.chmod(0o640)
    record = new_checkpoint_record(checkpoint_id, "turn", "session", "run", "turn", "", str(root.resolve()))
    record["file_entries"] = [{
        "path": "note.txt",
        "change_kind": "modified",
        "snapshot_eligible": True,
        "ineligible_reason": "",
        "before_exists": True,
        "before_blob_ref": before["blob_ref"],
        "before_hash": before["content_hash"],
        "before_mode": 0o600,
        "after_exists": True,
        "after_blob_ref": after["blob_ref"],
        "after_hash": after["content_hash"],
        "after_mode": 0o640,
        "expected_current_hash": after["content_hash"],
        "source_tool_change_ids": [],
    }]
    store.write_checkpoint_record(record)
    return target


def seed_unrelated_recovery_blocker(store, root, blocker_kind):
    if blocker_kind == "pending_tool":
        store.write_tool_change_record(new_tool_change_record(
            "tc_foreign_pending", "", "other-turn", "write_file", "workspace_write", "foreign"
        ))
        return
    if blocker_kind == "invalid_record":
        (store.tool_changes_dir / "foreign-invalid.json").write_bytes(b"{invalid")
        return
    record = new_checkpoint_record(
        "ckpt_foreign_" + blocker_kind,
        "restore",
        "session",
        "run",
        "other-turn",
        "",
        str(root.resolve()),
    )
    record["status"] = blocker_kind
    record["owner_id"] = "foreign"
    record["reviewed_at"] = ""
    store.write_checkpoint_record(record)


def test_applying_journal_contains_all_intents_before_first_mutation(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path, checkpoint_writer=RecoveryCheckpointWriter(store, tmp_path))
    observed = []

    def inspect_before_apply(restore_checkpoint_id, intent):
        journal = store.load_checkpoint_record(restore_checkpoint_id)
        observed.append((journal["status"], journal["restore_provenance"]["entries"]))
        target.write_bytes(store.read_blob(intent["planned_post_state"]["blob_ref"]))
        target.chmod(intent["planned_post_state"]["mode"])
        return {"outcome": "applied", "actual_post_state": intent["planned_post_state"]}

    monkeypatch.setattr(manager, "_apply_intent", inspect_before_apply)
    manager.apply_restore("ckpt_source")

    assert observed[0][0] == "applying"
    assert observed[0][1][0]["outcome"] == "pending"


def test_prestate_blob_failure_causes_zero_target_mutations(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    original = target.read_bytes()
    monkeypatch.setattr(store, "write_blob", lambda data, content_kind="text": (_ for _ in ()).throw(OSError("fsync failed")))

    with pytest.raises(OSError, match="fsync failed"):
        manager.apply_restore("ckpt_source")

    assert target.read_bytes() == original
    assert not [record for record in store.list_checkpoint_records() if record.get("checkpoint_type") == "restore"]


def test_target_parent_fsync_precedes_applied_outcome(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    events = []
    real_parent_fsync = manager._fsync_target_parent
    real_update = store.update_checkpoint_record

    def fsync_parent(path):
        events.append("target-parent-fsync")
        return real_parent_fsync(path)

    def update(checkpoint_id, transform, *, expected_status=None):
        result = real_update(checkpoint_id, transform, expected_status=expected_status)
        if any(entry.get("outcome") == "applied" for entry in result.get("restore_provenance", {}).get("entries", [])):
            events.append("applied-outcome")
        return result

    monkeypatch.setattr(manager, "_fsync_target_parent", fsync_parent)
    monkeypatch.setattr(store, "update_checkpoint_record", update)
    manager.apply_restore("ckpt_source")

    assert events.index("target-parent-fsync") < events.index("applied-outcome")


def test_successful_restore_applies_source_before_mode(tmp_path):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)

    result = manager.apply_restore("ckpt_source")

    assert result["status"] == "applied"
    assert target.read_bytes() == b"before"
    assert target.stat().st_mode & 0o777 == 0o600


def test_apply_lock_covers_plan_rebuild_journal_mutation_and_terminal_rmw(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    events = []
    held = {"value": False}
    real_plan = manager._preview_restore_locked
    real_apply = manager._apply_intent
    real_update = store.update_checkpoint_record

    @contextmanager
    def lock():
        held["value"] = True
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")
            held["value"] = False

    def plan(checkpoint_id):
        assert held["value"] is True
        events.append("strict-plan")
        return real_plan(checkpoint_id)

    def apply_intent(restore_checkpoint_id, intent):
        assert held["value"] is True
        events.append("target-mutation")
        return real_apply(restore_checkpoint_id, intent)

    def update(checkpoint_id, transform, *, expected_status=None):
        assert held["value"] is True
        result = real_update(checkpoint_id, transform, expected_status=expected_status)
        if result.get("status") in {"applied", "blocked", "failed", "partial", "noop"}:
            events.append("terminal-rmw")
        return result

    monkeypatch.setattr(store, "mutation_lock", lock)
    monkeypatch.setattr(manager, "_preview_restore_locked", plan)
    monkeypatch.setattr(manager, "_apply_intent", apply_intent)
    monkeypatch.setattr(store, "update_checkpoint_record", update)

    manager.apply_restore("ckpt_source")

    assert events[0] == "lock-enter"
    assert events.index("strict-plan") < events.index("target-mutation")
    assert events.index("target-mutation") < events.index("terminal-rmw")
    assert events[-1] == "lock-exit"


def test_apply_rebuilds_plan_and_blocks_post_preview_change(tmp_path):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    assert manager.preview_restore("ckpt_source")["status"] == "ready"
    target.write_bytes(b"external-after-preview")

    result = manager.apply_restore("ckpt_source")
    audit = store.load_checkpoint_record(result["restore_checkpoint_id"])

    assert result["status"] == "blocked"
    assert audit["status"] == "blocked"
    assert target.read_bytes() == b"external-after-preview"


def test_noop_apply_writes_successful_noop_audit(tmp_path):
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_noop", "turn", "session", "run", "turn", "", str(tmp_path.resolve())
    )
    store.write_checkpoint_record(record)
    manager = RecoveryManager(store, tmp_path)

    result = manager.apply_restore(record["checkpoint_id"])
    audit = store.load_checkpoint_record(result["restore_checkpoint_id"])

    assert result["status"] == "noop"
    assert audit["status"] == "noop"


def test_restore_journal_metadata_uses_store_redactor(tmp_path):
    store = CheckpointStore(tmp_path)
    _modified_checkpoint(store, tmp_path)
    sentinel = "sk-journal-owner-sentinel"

    def redact(value):
        return json.loads(json.dumps(value).replace(sentinel, "<redacted>"))

    store.set_redactor(redact)
    manager = RecoveryManager(store, tmp_path)
    manager.owner_id = sentinel
    result = manager.apply_restore("ckpt_source")
    raw = store._record_path(result["restore_checkpoint_id"]).read_bytes()

    assert sentinel.encode() not in raw


@pytest.mark.parametrize("blocker_kind", ("pending_tool", "invalid_record", "applying", "partial"))
def test_apply_restore_global_review_guard_blocks_before_plan_and_target(
    tmp_path, blocker_kind, monkeypatch
):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    seed_unrelated_recovery_blocker(store, tmp_path, blocker_kind)
    manager = RecoveryManager(store, tmp_path)
    calls = []
    monkeypatch.setattr(manager, "_apply_intent", lambda *args: calls.append(args))

    result = manager.apply_restore("ckpt_source")

    assert result["status"] == "blocked"
    assert target.read_bytes() == b"after"
    assert calls == []


def test_capture_blocked_result_never_creates_applying_or_mutates(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    monkeypatch.setattr(
        manager,
        "_capture_restore_intents",
        lambda plan: {"status": "blocked", "reason": "sensitive_content", "intents": []},
    )
    monkeypatch.setattr(
        manager,
        "_write_applying_journal",
        lambda *args: (_ for _ in ()).throw(AssertionError("applying journal written")),
    )
    monkeypatch.setattr(
        manager,
        "_apply_all_intents",
        lambda *args: (_ for _ in ()).throw(AssertionError("target mutation attempted")),
    )

    result = manager.apply_restore("ckpt_source")

    assert result["status"] == "blocked"
    assert target.read_bytes() == b"after"
    audit = store.load_checkpoint_record(result["restore_checkpoint_id"])
    assert audit["status"] == "blocked"
    assert not any(
        item.get("status") == "applying"
        for item in store.list_checkpoint_records(strict=True)
    )
```

- [ ] **Step 2: Run the tests and confirm RED**

```bash
./.venv/bin/python -m pytest tests/test_recovery_journal.py -q
```

Expected: `_apply_intent`, `_preview_restore_locked`, target fsync hooks, status-aware writer, full mutation-lock scope, blocked/noop audit, and journal redaction are absent or incomplete.

- [ ] **Step 3: Implement journal preparation and durable successful mutation**

Add top-level restore defaults `status`, `owner_id`, `reviewed_at`, `review_reason`, and `reviewed_by` when creating a restore checkpoint. Build every intent before creating the applying record:

```python
def _intent(path, pre_state, planned_post_state):
    return {
        "path": path,
        "pre_state": pre_state,
        "planned_post_state": planned_post_state,
        "outcome": "pending",
        "reason": "",
        "target_modified": False,
        "actual_post_state": {},
    }
```

`apply_restore()` must not call public preview before acquiring the lock. Its exact outer structure is:

```python
def apply_restore(self, checkpoint_id):
    with self.store.mutation_lock():
        try:
            tool_reviews = ToolChangeRecorder(
                self.store, owner_id="recovery-guard"
            ).pending_recovery_reviews()
            restore_reviews = self.pending_restore_reviews()
        except CheckpointStoreError:
            return self._write_recovery_review_blocked_audit(checkpoint_id)
        if tool_reviews or restore_reviews:
            return self._write_recovery_review_blocked_audit(checkpoint_id)
        plan = self._preview_restore_locked(checkpoint_id)
        if plan["status"] in {"invalid", "conflicted", "review_required"}:
            return self._write_non_mutating_restore_audit(plan, status="blocked")
        if plan["status"] == "noop":
            return self._write_non_mutating_restore_audit(plan, status="noop")
        capture = self._capture_restore_intents(plan)
        if capture["status"] == "blocked":
            return self._write_non_mutating_restore_audit(
                plan,
                status="blocked",
                reason=capture["reason"],
            )
        intents = capture["intents"]
        journal = self._write_applying_journal(plan, intents)
        self._apply_all_intents(journal["checkpoint_id"], intents)
        return self._finish_restore_journal(journal["checkpoint_id"])
```

The global guard is first inside the mutation lock. Import `ToolChangeRecorder` in `pico/recovery_manager.py` and instantiate the read-only guard locally from `self.store`; do not add a new `RecoveryManager` constructor dependency or assume an unbound `self.tool_change_recorder`. `pending_recovery_reviews()` and `pending_restore_reviews()` use strict enumeration, so any unrelated pending Tool Change, invalid mutation record, applying journal, or unreviewed partial writes a durable blocked audit and prevents plan/capture/target calls. A reviewed terminal partial is not a blocker; an unreviewed partial source blocks like every other partial.

`_preview_restore_locked()` strict-loads the source and rebuilds current tuples while the mutation lock is already held; it never reuses a previously returned plan. `_capture_restore_intents()` has a tagged return contract: status `ready` carries `reason=""` plus the complete intent list; status `blocked` carries a stable reason plus an empty intent list. For each pre-state it reads once, computes hash/mode from that read, calls `snapshot_bytes_eligibility()`, compares the observed tuple with the fresh ready plan tuple, then durably writes the eligible blob. Sensitive content or a changed tuple returns the blocked tag; `apply_restore()` writes a terminal blocked audit directly, creates no applying record, performs zero target mutation, and writes no ineligible raw blob.

Use this mutation durability contract in `RecoveryManager`:

```python
def _fsync_target_parent(self, path):
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_target_durable(self, path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=str(path.parent), prefix=path.name + ".restore.") as handle:
        temp_path = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temp_path.chmod(mode)
        if hash_file_bytes(temp_path)["content_hash"] != hash_bytes(data)["content_hash"]:
            raise OSError("temp_hash_mismatch")
        temp_path.replace(path)
        self._fsync_target_parent(path)
        observed = path.read_bytes()
        return {"exists": True, "hash": hash_bytes(observed)["content_hash"], "mode": path.stat().st_mode & 0o777}
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _delete_target_durable(self, path):
    path.unlink()
    self._fsync_target_parent(path)
    return {"exists": False, "hash": "", "blob_ref": "", "mode": None}
```

Immediately before `_write_target_durable()` or `_delete_target_durable()`, re-run lexical path/type and current hash/mode checks. Only after target parent fsync and reread match planned post-state may a durable, redacted checkpoint RMW set the entry outcome to `applied`. Finish with top-level `status="applied"` only when every intent is applied. The mutation lock is released only after the terminal journal write is durable.

- [ ] **Step 4: Run focused journal and manager regressions**

```bash
./.venv/bin/python -m pytest tests/test_recovery_journal.py tests/test_recovery_manager.py -q
./.venv/bin/python -m ruff check pico/recovery_models.py pico/recovery_checkpoint_writer.py pico/recovery_manager.py tests/test_recovery_journal.py
git diff --check
```

Expected: all commands exit 0 and event assertions prove journal-before-mutation and target-fsync-before-applied ordering.

- [ ] **Step 5: Review and commit Task 9**

Review lock→global strict Recovery Review guard→strict plan rebuild→pre-state/blob→durable applying journal→all target mutations/outcome RMWs→terminal RMW→unlock, tagged capture-blocked/noop audits, post-preview change blocking, metadata redaction, and zero target calls from blocked/non-ready plans.

```bash
git add pico/recovery_models.py pico/recovery_checkpoint_writer.py pico/recovery_manager.py tests/test_recovery_journal.py
git commit -m "feat(recovery): journal restore intents durably"
```

---

### Task 10: Reconcile Partial and Interrupted Restores and Expose Proven Undo

**Depends on:** Tasks 5 and 9.

**Files:**
- Modify: `pico/recovery_manager.py`
- Modify: `pico/recovery_checkpoint_writer.py`
- Modify: `tests/test_recovery_journal.py`
- Modify: `tests/test_recovery_e2e.py`

**Interfaces:**
- Consumes: Task 6 strict `pending_restore_reviews()` enumeration.
- Produces: read-only tuple reconciliation, explicit journal resolution, and proven-subset FileEntries.

```python
RecoveryManager.pending_restore_reviews() -> list[dict]
RecoveryManager.preview_restore_journal_resolution(checkpoint_id: str) -> dict
RecoveryManager.resolve_restore_journal(
    checkpoint_id: str,
    *,
    expected_record_hash: str,
    reviewed_by: str,
    review_reason: str,
) -> dict
```

- [ ] **Step 1: Add failing partial, crash, and undo tests**

```python
import pytest

from pico.checkpoint_store import CheckpointStore
from pico.recovery_checkpoint_writer import RecoveryCheckpointWriter
from pico.recovery_manager import RecoveryManager, RestoreMutationError
from pico.recovery_models import new_checkpoint_record


def _restorable_entry(store, path, before_bytes, after_bytes, *, before_mode=0o644, after_mode=0o644):
    before = store.write_blob(before_bytes)
    after = store.write_blob(after_bytes)
    return {
        "path": path,
        "change_kind": "modified",
        "snapshot_eligible": True,
        "ineligible_reason": "",
        "before_exists": True,
        "before_blob_ref": before["blob_ref"],
        "before_hash": before["content_hash"],
        "before_mode": before_mode,
        "after_exists": True,
        "after_blob_ref": after["blob_ref"],
        "after_hash": after["content_hash"],
        "after_mode": after_mode,
        "expected_current_hash": after["content_hash"],
        "source_tool_change_ids": [],
    }


def build_three_file_restore(tmp_path):
    store = CheckpointStore(tmp_path)
    writer = RecoveryCheckpointWriter(store, tmp_path)
    manager = RecoveryManager(store, tmp_path, checkpoint_writer=writer)
    record = new_checkpoint_record(
        "ckpt_three_files", "turn", "session", "run", "turn", "", str(tmp_path.resolve())
    )
    record["file_entries"] = [
        _restorable_entry(store, "first.txt", b"first-before", b"first-after"),
        _restorable_entry(store, "second.txt", b"second-before", b"second-after"),
        _restorable_entry(store, "third.txt", b"third-before", b"third-after"),
    ]
    (tmp_path / "first.txt").write_bytes(b"first-after")
    (tmp_path / "second.txt").write_bytes(b"second-after")
    (tmp_path / "third.txt").write_bytes(b"third-after")
    store.write_checkpoint_record(record)
    return store, manager, record["checkpoint_id"]


def build_created_file_restore(tmp_path):
    store = CheckpointStore(tmp_path)
    manager = RecoveryManager(store, tmp_path)
    target = tmp_path / "created.txt"
    target.write_bytes(b"created-after")
    after = store.write_blob(b"created-after")
    record = new_checkpoint_record(
        "ckpt_created", "turn", "session", "run", "turn", "", str(tmp_path.resolve())
    )
    record["file_entries"] = [{
        "path": "created.txt",
        "change_kind": "created",
        "snapshot_eligible": True,
        "ineligible_reason": "",
        "before_exists": False,
        "before_blob_ref": "",
        "before_hash": "",
        "before_mode": None,
        "after_exists": True,
        "after_blob_ref": after["blob_ref"],
        "after_hash": after["content_hash"],
        "after_mode": 0o644,
        "expected_current_hash": after["content_hash"],
        "source_tool_change_ids": [],
    }]
    store.write_checkpoint_record(record)
    return store, manager, record["checkpoint_id"], target


def build_applying_journal(tmp_path):
    store = CheckpointStore(tmp_path)
    manager = RecoveryManager(store, tmp_path)
    target = tmp_path / "note.txt"
    target.write_bytes(b"current-after")
    target.chmod(0o640)
    pre_blob = store.write_blob(b"current-after")
    post_blob = store.write_blob(b"restored-before")
    pre_state = {
        "exists": True,
        "hash": pre_blob["content_hash"],
        "blob_ref": pre_blob["blob_ref"],
        "mode": 0o640,
    }
    post_state = {
        "exists": True,
        "hash": post_blob["content_hash"],
        "blob_ref": post_blob["blob_ref"],
        "mode": 0o600,
    }
    record = new_checkpoint_record(
        "ckpt_applying", "restore", "session", "run", "turn", "", str(tmp_path.resolve())
    )
    record["status"] = "applying"
    record["owner_id"] = "owner-crashed"
    record["restore_provenance"] = {
        "source_checkpoint_id": "ckpt_source",
        "plan_id": "plan_crashed",
        "entries": [{
            "path": "note.txt",
            "pre_state": pre_state,
            "planned_post_state": post_state,
            "outcome": "pending",
            "reason": "",
            "target_modified": False,
            "actual_post_state": {},
        }],
    }
    store.write_checkpoint_record(record)
    return store, manager, record["checkpoint_id"], target, post_state


def build_partial_restore_with_one_proven_entry(tmp_path):
    store = CheckpointStore(tmp_path)
    manager = RecoveryManager(store, tmp_path)
    entry = _restorable_entry(store, "note.txt", b"current-c", b"restored-a")
    target = tmp_path / "note.txt"
    target.write_bytes(b"restored-a")
    record = new_checkpoint_record(
        "ckpt_partial", "restore", "session", "run", "turn", "", str(tmp_path.resolve())
    )
    record["status"] = "partial"
    record["reviewed_at"] = "2026-07-10T00:00:00+00:00"
    record["reviewed_by"] = "cli"
    record["review_reason"] = "explicit_cli_resolution"
    record["file_entries"] = [entry]
    record["restore_provenance"] = {"entries": []}
    store.write_checkpoint_record(record)
    return store, manager, record["checkpoint_id"], target


def test_second_entry_failure_is_partial_with_not_attempted_tail(tmp_path, monkeypatch):
    store, manager, checkpoint_id = build_three_file_restore(tmp_path)
    real_apply = manager._apply_intent
    calls = {"count": 0}

    def fail_second(restore_checkpoint_id, intent):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("second write failed")
        return real_apply(restore_checkpoint_id, intent)

    monkeypatch.setattr(manager, "_apply_intent", fail_second)
    result = manager.apply_restore(checkpoint_id)
    journal = store.load_checkpoint_record(result["restore_checkpoint_id"])

    assert result["status"] == "partial"
    assert [entry["outcome"] for entry in journal["restore_provenance"]["entries"]] == [
        "applied",
        "failed",
        "not_attempted",
    ]
    assert len(journal["file_entries"]) == 1


def test_replace_success_then_outcome_crash_stays_applying_and_reconciles(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)

    def crash_before_outcome(restore_checkpoint_id, index, outcome):
        raise KeyboardInterrupt("crash before outcome RMW")

    monkeypatch.setattr(manager, "_write_intent_outcome", crash_before_outcome)
    with pytest.raises(KeyboardInterrupt, match="outcome RMW"):
        manager.apply_restore("ckpt_source")

    journal = next(
        record for record in store.list_checkpoint_records()
        if record.get("checkpoint_type") == "restore"
    )
    assert target.read_bytes() == b"before"
    assert journal["status"] == "applying"
    assert journal["restore_provenance"]["entries"][0]["outcome"] == "pending"
    preview = manager.preview_restore_journal_resolution(journal["checkpoint_id"])
    assert preview["entries"][0]["classification"] == "applied_unconfirmed"


def test_unlink_success_then_outcome_crash_reconciles_absent_post_state(tmp_path, monkeypatch):
    store, manager, source_id, target = build_created_file_restore(tmp_path)
    monkeypatch.setattr(
        manager,
        "_write_intent_outcome",
        lambda restore_checkpoint_id, index, outcome: (_ for _ in ()).throw(
            KeyboardInterrupt("crash after unlink")
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="after unlink"):
        manager.apply_restore(source_id)

    journal = next(
        record for record in store.list_checkpoint_records()
        if record.get("checkpoint_type") == "restore"
    )
    assert not target.exists()
    preview = manager.preview_restore_journal_resolution(journal["checkpoint_id"])
    assert preview["entries"][0]["classification"] == "applied_unconfirmed"


def test_delete_reread_detects_reappeared_target_as_uncertain_partial(tmp_path, monkeypatch):
    store, manager, source_id, target = build_created_file_restore(tmp_path)
    real_fsync = manager._fsync_target_parent

    def recreate_after_fsync(path):
        real_fsync(path)
        target.write_bytes(b"external-recreated")

    monkeypatch.setattr(manager, "_fsync_target_parent", recreate_after_fsync)
    result = manager.apply_restore(source_id)
    journal = store.load_checkpoint_record(result["restore_checkpoint_id"])

    assert result["status"] == "partial"
    assert journal["restore_provenance"]["entries"][0]["outcome"] == "uncertain"
    assert journal["restore_provenance"]["entries"][0]["target_modified"] is True


def test_target_modified_sensitive_actual_post_is_not_blobbed(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    target = _modified_checkpoint(store, tmp_path)
    manager = RecoveryManager(store, tmp_path)
    sentinel = b"sk-sensitive-actual-post-value"

    def corrupt_then_fail(path, data, mode):
        path.write_bytes(sentinel)
        raise RestoreMutationError("reread_hash_mismatch", target_modified=True)

    monkeypatch.setattr(manager, "_write_target_durable", corrupt_then_fail)
    result = manager.apply_restore("ckpt_source")
    journal = store.load_checkpoint_record(result["restore_checkpoint_id"])
    intent = journal["restore_provenance"]["entries"][0]

    assert result["status"] == "partial"
    assert intent["outcome"] == "uncertain"
    assert intent["reason"] == "manual_recovery_required"
    assert intent["actual_post_state"] == {}
    assert all(sentinel not in path.read_bytes() for path in store.blobs_dir.rglob("*") if path.is_file())


def test_applying_journal_reconciles_actual_tuple_without_writing(tmp_path):
    store, manager, restore_id, target, post_state = build_applying_journal(tmp_path)
    target.write_bytes(store.read_blob(post_state["blob_ref"]))
    target.chmod(post_state["mode"])
    before = store.load_checkpoint_record(restore_id)

    preview = manager.preview_restore_journal_resolution(restore_id)
    after = store.load_checkpoint_record(restore_id)

    assert preview["entries"][0]["classification"] == "applied_unconfirmed"
    assert before == after


def test_resolve_unknown_tuple_is_partial_manual_recovery(tmp_path):
    store, manager, restore_id, target, post_state = build_applying_journal(tmp_path)
    target.write_bytes(b"external-value")

    preview = manager.preview_restore_journal_resolution(restore_id)
    resolved = manager.resolve_restore_journal(
        restore_id,
        expected_record_hash=preview["record_hash"],
        reviewed_by="cli",
        review_reason="explicit_cli_resolution",
    )

    assert resolved["status"] == "partial"
    assert resolved["restore_provenance"]["entries"][0]["outcome"] == "uncertain"
    assert resolved["restore_provenance"]["entries"][0]["reason"] == "manual_recovery_required"


def test_partial_checkpoint_undoes_only_proven_file_entries(tmp_path):
    store, manager, partial_id, target = build_partial_restore_with_one_proven_entry(tmp_path)
    partial = store.load_checkpoint_record(partial_id)

    assert partial["status"] == "partial"
    assert len(partial["file_entries"]) == 1
    undo = manager.apply_restore(partial_id)
    assert undo["status"] == "applied"
    assert target.read_bytes() == b"current-c"


def test_partial_requires_explicit_preview_then_apply_acceptance(tmp_path):
    store, manager, partial_id, target = build_partial_restore_with_one_proven_entry(tmp_path)
    record = store.load_checkpoint_record(partial_id)
    record["reviewed_at"] = ""
    record["reviewed_by"] = ""
    record["review_reason"] = ""
    store.write_checkpoint_record(record)

    before = store.load_checkpoint_record(partial_id)
    preview = manager.preview_restore_journal_resolution(partial_id)
    assert preview["status"] == "partial_review_required"
    assert store.load_checkpoint_record(partial_id) == before

    accepted = manager.resolve_restore_journal(
        partial_id,
        expected_record_hash=preview["record_hash"],
        reviewed_by="cli",
        review_reason="explicit_cli_resolution",
    )
    assert accepted["status"] == "partial"
    assert accepted["reviewed_at"]
    assert target.read_bytes() == b"restored-a"
```

- [ ] **Step 2: Run focused reconciliation tests and confirm RED**

```bash
./.venv/bin/python -m pytest tests/test_recovery_journal.py tests/test_recovery_e2e.py -q
```

Expected: reconciliation interfaces, `_write_intent_outcome`, delete reread, target-modified classification, explicit partial acceptance, or proven undo behavior is missing.

- [ ] **Step 3: Implement deterministic tuple classification and terminal mapping**

Use this exact classifier:

```python
def _classify_observed_state(observed, pre_state, planned_post_state):
    observed_tuple = (observed["exists"], observed["hash"], observed["mode"])
    pre_tuple = (pre_state["exists"], pre_state["hash"], pre_state["mode"])
    post_tuple = (
        planned_post_state["exists"],
        planned_post_state["hash"],
        planned_post_state["mode"],
    )
    if observed_tuple == post_tuple:
        return "applied_unconfirmed"
    if observed_tuple == pre_tuple:
        return "not_applied"
    return "manual_recovery_required"


class RestoreMutationError(OSError):
    def __init__(self, code, *, target_modified):
        super().__init__(code)
        self.code = str(code)
        self.target_modified = bool(target_modified)
```

Split target mutation from journal outcome persistence: `_apply_intent()` returns an outcome and `_write_intent_outcome(restore_checkpoint_id, index, outcome)` performs the durable redacted RMW. This explicit seam is required for real replace/unlink-success-before-outcome crash tests. Do not catch `KeyboardInterrupt`/process interruption between these calls; the durable intent remains `pending` for reconciliation.

After `unlink()` and parent fsync, `_delete_target_durable()` must `lstat()` the target again. Absence is success; a reappeared path is `RestoreMutationError("delete_reread_mismatch", target_modified=True)` and therefore uncertain/partial.

`preview_restore_journal_resolution()` reads only and returns classifications plus SHA-256 `record_hash` of the exact JSON bytes observed. `resolve_restore_journal()` holds the mutation lock, reloads under the store lock, requires the same raw record hash and expected status, recomputes classifications, and maps applying intents to `applied`, `not_attempted`, or `uncertain`. A changed record fails `record_changed` without writing. For an already-terminal unreviewed partial, preview returns `partial_review_required`; explicit apply acceptance writes only reviewed metadata, leaves status/outcomes unchanged, and performs no workspace mutation. Overall status is `applied` when all are applied, `failed` when none changed and none are uncertain, and `partial` otherwise. After a failure, every untouched tail intent becomes `not_attempted` in the same terminal RMW.

For each proven applied mutation, generate a FileEntry whose before-state is journal `pre_state`, after-state is actual planned post-state, expected current hash is the post hash, and source Tool Change IDs are empty. Do not generate a FileEntry for uncertain or not-attempted outcomes. When `RestoreMutationError.target_modified` is true, read actual post bytes once and run path/content eligibility on those exact bytes before writing a blob. Sensitive, inconsistent, unreadable, or type-invalid actual post leaves `actual_post_state={}`, sets `outcome="uncertain"`, `reason="manual_recovery_required"`, and overall `partial`.

- [ ] **Step 4: Run focused, manager, and E2E tests**

```bash
./.venv/bin/python -m pytest tests/test_recovery_journal.py tests/test_recovery_manager.py tests/test_recovery_e2e.py -q
./.venv/bin/python -m ruff check pico/recovery_manager.py pico/recovery_checkpoint_writer.py tests/test_recovery_journal.py tests/test_recovery_e2e.py
git diff --check
```

Expected: all commands exit 0; preview is byte-for-byte read-only and an applying journal is rejected as a restore source.

- [ ] **Step 5: Review and commit Task 10**

Review real replace/unlink interruption windows, delete reread, `target_modified`, same-read actual-post eligibility, sensitive zero-blob behavior, uncertain partial, tail `not_attempted`, explicit partial preview/apply acceptance, zero-mutation `failed`, zero-mutation conflict `blocked`, verified `applied`, empty `noop`, and proven-subset undo.

```bash
git add pico/recovery_manager.py pico/recovery_checkpoint_writer.py tests/test_recovery_journal.py tests/test_recovery_e2e.py
git commit -m "feat(recovery): reconcile partial restores"
```

---

### Task 11: Recovery Review CLI, Resolution, and Quarantine Surface

**Depends on:** Tasks 3, 5, and 10.

**Files:**
- Modify: `pico/cli.py`
- Modify: `pico/cli_recovery.py`
- Modify: `pico/cli_commands.py`
- Modify: `pico/recovery_manager.py`
- Modify: `pico/runtime.py`
- Modify: `tests/test_recovery_cli.py`
- Modify: `tests/test_tool_change_recorder.py`

**Interfaces:**
- Produces these exact commands:

```text
pico-cli --cwd <root> checkpoints pending
pico-cli --cwd <root> checkpoints resolve-pending <record-or-opaque-id>
pico-cli --cwd <root> checkpoints resolve-pending <record-or-opaque-id> --apply
```

- Produces this A3-consumed inspection-only interface:

```python
collect_recovery_review_items(store, workspace_root) -> dict

RECOVERY_REVIEW_KEYS = (
    "tool_changes",
    "restore_journals",
    "invalid_records",
    "quarantined_records",
)
```

It uses non-strict enumeration so malformed evidence becomes opaque invalid items. Mutation guard code must not call it; guards continue to use strict enumeration and fail closed.

- [ ] **Step 1: Write failing CLI preview, resolution, and output tests**

```python
import json
import os

from pico.checkpoint_store import CheckpointStore
from pico.cli import COMMAND_SPECS, main
from pico.recovery_manager import RecoveryManager, collect_recovery_review_items
from pico.tool_change_recorder import ToolChangeRecorder


def test_checkpoints_pending_lists_tool_change_and_invalid_record(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    ToolChangeRecorder(store, owner_id="owner-a").start(
        "", "turn-1", "write_file", "workspace_write", {"path": "note.txt"}
    )
    secret_filename = "github_pat_secret_filename.json"
    (store.tool_changes_dir / secret_filename).write_bytes(b"{private-invalid-evidence")

    code = main(["--cwd", str(tmp_path), "--format", "json", "checkpoints", "pending"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["kind"] == "checkpoints_pending"
    assert {item["status"] for item in payload["data"]["tool_changes"]} == {"pending"}
    assert {item["status"] for item in payload["data"]["invalid_records"]} == {"invalid_record"}
    assert "private-invalid-evidence" not in json.dumps(payload)
    assert "github_pat_secret_filename" not in json.dumps(payload)
    assert payload["data"]["invalid_records"][0]["opaque_id"].startswith("invalid_")


def test_collect_recovery_review_items_has_fixed_read_only_shape(tmp_path):
    store = CheckpointStore(tmp_path)
    ToolChangeRecorder(store, owner_id="owner-a").start(
        "", "turn-1", "write_file", "workspace_write", {}
    )
    (store.records_dir / "secret-filename.json").write_bytes(b"{invalid-private-bytes")
    before = {
        path: path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }

    items = collect_recovery_review_items(store, tmp_path)
    after = {
        path: path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }

    assert set(items) == {
        "tool_changes",
        "restore_journals",
        "invalid_records",
        "quarantined_records",
    }
    assert items["tool_changes"][0]["status"] == "pending"
    assert items["restore_journals"] == []
    assert items["invalid_records"][0]["opaque_id"].startswith("invalid_")
    assert items["quarantined_records"] == []
    assert "secret-filename" not in json.dumps(items)
    assert "invalid-private-bytes" not in json.dumps(items)
    assert before == after


def test_command_specs_register_recovery_review_subcommands():
    assert {"pending", "resolve-pending"} <= COMMAND_SPECS["checkpoints"]["subcommands"]


def test_resolve_pending_defaults_to_read_only_preview(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    pending = ToolChangeRecorder(store, owner_id="owner-a").start(
        "", "turn-1", "write_file", "workspace_write", {}
    )

    code = main([
        "--cwd", str(tmp_path), "--format", "json",
        "checkpoints", "resolve-pending", pending["tool_change_id"],
    ])

    assert code == 0
    assert store.load_tool_change_record(pending["tool_change_id"])["status"] == "pending"


def test_resolve_pending_apply_interrupts_with_review_metadata(tmp_path):
    store = CheckpointStore(tmp_path)
    pending = ToolChangeRecorder(store, owner_id="owner-a").start(
        "", "turn-1", "write_file", "workspace_write", {}
    )

    code = main([
        "--cwd", str(tmp_path), "checkpoints", "resolve-pending", pending["tool_change_id"], "--apply"
    ])
    record = store.load_tool_change_record(pending["tool_change_id"])

    assert code == 0
    assert record["status"] == "interrupted"
    assert record["reviewed_by"] == "cli"


def test_resolve_invalid_apply_quarantines_without_deleting_bytes(tmp_path):
    store = CheckpointStore(tmp_path)
    raw = b"{private-invalid-evidence"
    source = store.records_dir / "secret-token-filename.json"
    source.write_bytes(raw)
    [invalid] = store.list_checkpoint_records(strict=False)

    preview_code = main([
        "--cwd", str(tmp_path), "checkpoints", "resolve-pending", invalid["opaque_id"]
    ])
    assert preview_code == 0
    assert source.read_bytes() == raw

    apply_code = main([
        "--cwd", str(tmp_path), "checkpoints", "resolve-pending", invalid["opaque_id"], "--apply"
    ])
    assert apply_code == 0
    inspected = store.list_quarantined_records()[0]
    assert inspected["opaque_id"] == invalid["opaque_id"]
    assert inspected["status"] == "quarantined"
    assert "secret-token-filename" not in json.dumps(inspected)
    assert (store.root / inspected["quarantine_raw_path"]).read_bytes() == raw


def test_resolve_non_regular_invalid_apply_moves_inode_without_following(tmp_path):
    store = CheckpointStore(tmp_path)
    outside = tmp_path / "outside-private"
    outside.write_bytes(b"must-not-be-read-or-moved")
    source = store.records_dir / "linked.json"
    source.symlink_to(outside)
    [invalid] = store.list_checkpoint_records(strict=False)

    preview_code = main([
        "--cwd", str(tmp_path), "checkpoints", "resolve-pending", invalid["opaque_id"]
    ])
    assert preview_code == 0
    assert os.path.lexists(source)

    apply_code = main([
        "--cwd", str(tmp_path), "checkpoints", "resolve-pending",
        invalid["opaque_id"], "--apply",
    ])
    assert apply_code == 0
    assert not os.path.lexists(source)
    inspected = store.list_quarantined_records()[0]
    evidence = store.root / inspected["quarantine_evidence_path"]
    assert evidence.is_symlink()
    assert os.readlink(evidence) == str(outside)
    assert outside.read_bytes() == b"must-not-be-read-or-moved"


def test_quarantined_record_remains_visible_as_inactive_inspection(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    (store.records_dir / "secret-filename.json").write_bytes(b"{invalid")
    [invalid] = store.list_checkpoint_records(strict=False)
    store.quarantine_invalid_record(invalid["opaque_id"], expected_raw_hash=invalid["raw_hash"])

    code = main(["--cwd", str(tmp_path), "--format", "json", "checkpoints", "pending"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert any(
        item.get("opaque_id") == invalid["opaque_id"] and item.get("status") == "quarantined"
        for item in payload["data"]["quarantined_records"]
    )
    assert all(
        item.get("opaque_id") != invalid["opaque_id"]
        for item in payload["data"]["invalid_records"]
    )


def test_partial_review_requires_preview_then_explicit_apply_acceptance(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    record = new_checkpoint_record(
        "ckpt_partial_review", "restore", "session", "run", "turn", "", str(tmp_path.resolve())
    )
    record["status"] = "partial"
    record["restore_provenance"] = {
        "entries": [{
            "path": "note.txt",
            "pre_state": {"exists": False, "hash": "", "blob_ref": "", "mode": None},
            "planned_post_state": {"exists": False, "hash": "", "blob_ref": "", "mode": None},
            "outcome": "uncertain",
            "reason": "manual_recovery_required",
            "target_modified": True,
            "actual_post_state": {},
        }]
    }
    store.write_checkpoint_record(record)

    preview_code = main([
        "--cwd", str(tmp_path), "--format", "json",
        "checkpoints", "resolve-pending", record["checkpoint_id"],
    ])
    preview = json.loads(capsys.readouterr().out)
    assert preview_code == 0
    assert preview["data"]["status"] == "partial_review_required"
    assert store.load_checkpoint_record(record["checkpoint_id"])["reviewed_at"] == ""

    apply_code = main([
        "--cwd", str(tmp_path), "checkpoints", "resolve-pending", record["checkpoint_id"], "--apply"
    ])
    accepted = store.load_checkpoint_record(record["checkpoint_id"])
    assert apply_code == 0
    assert accepted["status"] == "partial"
    assert accepted["reviewed_at"]
    assert accepted["restore_provenance"]["entries"][0]["outcome"] == "uncertain"


def test_blocked_and_partial_restore_apply_return_runtime_exit(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    checkpoint = write_restorable_checkpoint(store, tmp_path, "ckpt_exit")
    (tmp_path / "note.txt").write_text("external\n", encoding="utf-8")
    blocked = main([
        "--cwd", str(tmp_path), "checkpoints", "restore", checkpoint["checkpoint_id"], "--apply"
    ])
    assert blocked == 1

    monkeypatch.setattr(
        RecoveryManager,
        "apply_restore",
        lambda self, checkpoint_id: {
            "status": "partial",
            "restore_checkpoint_id": "ckpt_partial_result",
        },
    )
    partial = main([
        "--cwd", str(tmp_path), "checkpoints", "restore", checkpoint["checkpoint_id"], "--apply"
    ])
    assert partial == 1
```

- [ ] **Step 2: Run CLI tests and confirm RED**

```bash
./.venv/bin/python -m pytest tests/test_recovery_cli.py tests/test_tool_change_recorder.py -q
```

Expected: `COMMAND_SPECS` and CLI reject Recovery Review subcommands, `--format json` coverage is missing, invalid opaque resolution/quarantine inspection is absent, startup still auto-interrupts, or blocked/partial restore incorrectly exits 0.

- [ ] **Step 3: Implement command routing and stable resolution behavior**

Extend `handle_checkpoints()` with exact branches:

```python
if sub == "pending" and not rest:
    data = collect_recovery_review_items(store, root)
    return print_result("checkpoints_pending", data, args, _render_pending_reviews)

if sub == "resolve-pending" and _is_resolve_pending_args(rest):
    record_id = rest[0]
    apply_flag = "--apply" in rest[1:]
    result = _resolve_pending_record(store, root, record_id, apply_flag=apply_flag)
    return print_result("checkpoints_resolve_pending", result, args, _render_json_body)
```

In `pico/cli.py`, add `pending` and `resolve-pending` to `COMMAND_SPECS["checkpoints"]["subcommands"]`; do not add a parallel parser or legacy JSON shortcut. All JSON tests and documented commands use the existing `--format json` contract.

Update `tests/test_recovery_cli.py::write_restorable_checkpoint()` to emit the complete Task 4 FileEntry fields, including before/after existence, modes, matching blob/hash pairs, expected/after equality, and `source_tool_change_ids=[]`; CLI tests must not depend on legacy downgrade behavior unless the test explicitly names a legacy reason.

Implement `collect_recovery_review_items()` in `pico/recovery_manager.py`. It non-strict lists both active record kinds, puts validated pending Tool Changes in `tool_changes`, validated `applying` and unreviewed `partial` restore records in `restore_journals`, opaque active-record placeholders in `invalid_records`, and validated quarantine metadata only in `quarantined_records`. It returns exactly those four keys, writes nothing, exposes no raw filename/path/bytes, and applies only inspection-safe summaries. A quarantined item must never also appear in `invalid_records`. `ToolExecutor` mutation guard continues to call strict `pending_recovery_reviews()`/`pending_restore_reviews()` and must never use this inspection helper.

`_resolve_pending_record()` resolves a safe Tool Change/checkpoint ID or opaque invalid ID. Preview returns current status/classifications and exact record/evidence hash without writing. Apply uses that preview hash, then delegates to owner-safe Tool Change CAS, restore journal record-hash CAS, or `quarantine_invalid_record(opaque_id, expected_raw_hash=preview["raw_hash"])`; each delegate acquires mutation→store locks and re-enumerates/reloads before writing. The opaque invalid branch handles both regular raw records and non-regular/symlink evidence, so explicit `--apply` always removes the reviewed blocker from the active store without opening or following it. Use `reviewed_by="cli"` and `review_reason="explicit_cli_resolution"`. An unreviewed terminal partial is accepted only by explicit `--apply`; acceptance changes reviewed metadata only and leaves workspace/status/outcomes unchanged. Ambiguous, missing, or changed IDs return stable errors. Remove runtime startup auto-interruption and put active review count/opaque IDs in status/report.

`checkpoints restore <id> --apply` returns `CLI_EXIT_RUNTIME` (1) for `blocked`, `failed`, or `partial`, and success (0) only for `applied` or `noop`. `resolve-pending --apply` returns 0 when the requested review/quarantine action itself succeeds, even when an accepted journal remains terminal `partial`.

- [ ] **Step 4: Run CLI, lifecycle, and mutation regressions**

```bash
./.venv/bin/python -m pytest tests/test_recovery_cli.py tests/test_tool_change_recorder.py tests/test_tool_executor_mutation_lock.py -q
./.venv/bin/python -m ruff check pico/cli_recovery.py pico/cli_commands.py pico/runtime.py tests/test_recovery_cli.py tests/test_tool_change_recorder.py
git diff --check
```

Expected: all commands exit 0; previews do not change file hashes or record status.

- [ ] **Step 5: Review and commit Task 11**

Review the fixed cross-plan collection shape, inspection-only/non-strict versus mutation-guard/strict separation, `COMMAND_SPECS`, exclusive `--format json`, opaque output, absent raw filenames/paths/bytes, preview/hash-CAS apply, partial acceptance, blocked/partial nonzero restore exit, post-quarantine inspection, metadata redaction, and `already_resolved` terminal protection.

```bash
git add pico/cli.py pico/cli_recovery.py pico/cli_commands.py pico/recovery_manager.py pico/runtime.py tests/test_recovery_cli.py tests/test_tool_change_recorder.py
git commit -m "feat(cli): add recovery review commands"
```

---

### Task 12: Protect Prune References and Pass the A2 Integration Gate

**Depends on:** Tasks 3 and 6 through 11.

**Files:**
- Modify: `pico/checkpoint_store.py`
- Modify: `tests/test_checkpoint_store_phase1.py`
- Modify: `tests/test_recovery_e2e.py`
- Modify: `tests/test_recovery_cli.py`

**Interfaces:**
- Completes: existing `CheckpointStore.prune(dry_run=True, older_than=None, now=None) -> dict` under mutation-lock/store-lock ordering.

- [ ] **Step 1: Write failing prune-reference and lifecycle E2E tests**

```python
def test_prune_preserves_prepared_and_restore_intent_blobs(tmp_path):
    store = CheckpointStore(tmp_path)
    prepared = store.write_blob(b"prepared")
    intent_pre = store.write_blob(b"intent-pre")
    intent_post = store.write_blob(b"intent-post")
    tool = new_tool_change_record("tc_refs", "", "turn", "write_file", "workspace_write", "owner")
    tool["prepared_file_entries"] = [{"path": "a.txt", "before_blob_ref": prepared["blob_ref"]}]
    store.write_tool_change_record(tool)
    checkpoint = new_checkpoint_record("ckpt_refs", "restore", "", "", "", "", str(tmp_path.resolve()))
    checkpoint["status"] = "applying"
    checkpoint["restore_provenance"] = {
        "entries": [{
            "path": "a.txt",
            "pre_state": {"exists": True, "hash": intent_pre["content_hash"], "blob_ref": intent_pre["blob_ref"], "mode": 0o644},
            "planned_post_state": {"exists": True, "hash": intent_post["content_hash"], "blob_ref": intent_post["blob_ref"], "mode": 0o644},
            "outcome": "pending",
            "reason": "",
            "target_modified": False,
            "actual_post_state": {},
        }]
    }
    store.write_checkpoint_record(checkpoint)

    result = store.prune(dry_run=True)

    assert prepared["blob_ref"] not in result["unreferenced_blob_refs"]
    assert intent_pre["blob_ref"] not in result["unreferenced_blob_refs"]
    assert intent_post["blob_ref"] not in result["unreferenced_blob_refs"]


def test_recovery_e2e_a_b_c_restore_review_and_undo(tmp_path):
    agent = build_agent(tmp_path, [])
    (tmp_path / "note.txt").write_text("A", encoding="utf-8")
    first = agent.run_tool("write_file", {"path": "note.txt", "content": "B"})
    first_id = first.metadata["tool_change_id"]
    second = agent.run_tool("write_file", {"path": "note.txt", "content": "C"})
    second_id = second.metadata["tool_change_id"]
    checkpoint = agent.recovery_checkpoint_writer.create_turn_checkpoint(
        session_id="session",
        run_id="run",
        turn_id="turn",
        parent_checkpoint_id="",
        tool_change_ids=[first_id, second_id],
    )

    restored = agent.recovery_manager.apply_restore(checkpoint["checkpoint_id"])
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "A"
    undone = agent.recovery_manager.apply_restore(restored["restore_checkpoint_id"])
    assert undone["status"] == "applied"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "C"
```

- [ ] **Step 2: Run focused prune and E2E tests and confirm RED**

```bash
./.venv/bin/python -m pytest tests/test_checkpoint_store_phase1.py tests/test_recovery_e2e.py tests/test_recovery_cli.py -q
```

Expected: nested journal/prepared blob refs are reported unreferenced, or A→B→C restore/undo does not round-trip.

- [ ] **Step 3: Extend reference scanning and hold locks for the whole prune transaction**

Add this recursive state-ref owner and call it for Tool Change prepared entries and every journal intent state:

```python
def _collect_state_blob_ref(state, sink):
    if not isinstance(state, dict):
        return
    value = state.get("blob_ref")
    if isinstance(value, str) and value:
        sink.add(value)


def _collect_journal_refs(record, sink):
    provenance = record.get("restore_provenance") or {}
    for intent in provenance.get("entries", []) or []:
        _collect_state_blob_ref(intent.get("pre_state"), sink)
        _collect_state_blob_ref(intent.get("planned_post_state"), sink)
        _collect_state_blob_ref(intent.get("actual_post_state"), sink)
```

`prune()` must acquire `mutation_lock()` before the store lock, strict-enumerate active records while both locks are held, calculate references and candidates from that same snapshot, and perform deletions before releasing either lock. To avoid nested store-lock deadlock, use private unlocked list/write helpers only inside the already-held store lock.

- [ ] **Step 4: Run the complete A2 focused verification**

```bash
./.venv/bin/python -m pytest \
  tests/test_file_lock.py \
  tests/test_artifact_security.py \
  tests/test_checkpoint_store_security.py \
  tests/test_checkpoint_store_durability.py \
  tests/test_checkpoint_store_invalid_records.py \
  tests/test_checkpoint_store_phase1.py \
  tests/test_recovery_policy.py \
  tests/test_recovery_models.py \
  tests/test_tool_change_recorder.py \
  tests/test_tool_executor_mutation_lock.py \
  tests/test_tool_executor.py \
  tests/test_recovery_checkpoint_writer.py \
  tests/test_recovery_paths.py \
  tests/test_recovery_manager.py \
  tests/test_recovery_journal.py \
  tests/test_recovery_cli.py \
  tests/test_recovery_e2e.py -q
./.venv/bin/python -m ruff check pico tests
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 5: Run the full local gate and commit Task 12**

```bash
./scripts/check.sh
```

Expected: Ruff exits 0 and the complete pytest suite exits 0.

Review the final diff against design §§9.1–9.7 and DoD items 4, 6, 7, and 8. Confirm no new dependency, compatibility schema, raw-runner path, or automatic pending cleanup was introduced.

```bash
git add pico/checkpoint_store.py tests/test_checkpoint_store_phase1.py tests/test_recovery_e2e.py tests/test_recovery_cli.py
git commit -m "feat(recovery): complete secure recovery lifecycle"
```

## Dependency Order

```text
Task 1 → Task 2 → Task 3
Task 1 + A1 → Task 4
Task 2 + Task 3 → Task 5
Task 2 + Task 3 + Task 4 + Task 5 → Task 6
Task 4 + Task 5 → Task 7
Task 1 + Task 3 + Task 7 → Task 8
Task 2 + Task 4 + Task 8 → Task 9
Task 5 + Task 9 → Task 10
Task 3 + Task 5 + Task 10 → Task 11
Task 3 + Task 6 + Task 7 + Task 8 + Task 9 + Task 10 + Task 11 → Task 12
```

Tasks 2 and 4 may run in parallel after Task 1. Tasks 5 and 7 must not run in parallel because both modify record contracts. Tasks 9 through 12 are sequential because each consumes the exact journal fields produced by its predecessor.

## Plan Self-Review Checklist

- [ ] Every A2 design item has a task: ID/ref/hash validation, private durability, strict enumeration, quarantine, mutation lock, complete FileEntry, coalescing, plan validation, journal-first apply, partial reconciliation, pending review, undo, and prune.
- [ ] Every production behavior starts with a named failing test and an exact RED command.
- [ ] Every task has exact files, consumed/produced interfaces, GREEN commands, review criteria, and an independent commit.
- [ ] FileEntry, journal intent, status, outcome, and CLI names are consistent across all tasks.
- [ ] Invalid evidence uses only `invalid_<sha256(internal store-relative path + raw-or-lstat evidence + kind)>`, exposes no raw stem/path/link target, distinguishes duplicate bytes at different paths, and quarantine apply re-enumerates under mutation→store locks with raw/evidence-hash plus inode CAS.
- [ ] `apply_restore()` holds the mutation lock from strict plan rebuild through terminal journal durability; real replace/unlink crash windows and delete reread are tested.
- [ ] FileEntry/plan checks include blob-ref/hash equality, expected/after equality, kind/exists transitions, source Tool Change terminal/internal identity, and both legacy downgrade reasons.
- [ ] A1's `CheckpointStore(workspace_root, redactor=None)`, `set_redactor()`, private no-follow `locked_file()`, JSON redaction, exact blobs, and exact quarantine raw bytes remain intact.
- [ ] `collect_recovery_review_items()` has exactly `tool_changes/restore_journals/invalid_records/quarantined_records`; validated quarantine metadata appears only in `quarantined_records`; the helper is inspection-only/non-strict and is never used by strict mutation guards.
- [ ] CLI registration is in `COMMAND_SPECS`, JSON uses only `--format json`, partial acceptance is explicit preview/apply, and blocked/partial restore returns runtime exit 1.
- [ ] The plan contains no speculative plugin, registry, policy hierarchy, new dependency, Windows implementation, or filesystem transaction claim.
- [ ] Final verification includes focused recovery tests, Ruff, `git diff --check`, and `./scripts/check.sh`.
