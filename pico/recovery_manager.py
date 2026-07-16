"""RecoveryManager 骨架：负责基于 Checkpoint Record 做恢复预览和应用。

Phase 1 只支持“回到某个 turn 之前的原始字节状态”这一种恢复。核心不变式：
    - 只对 file_entries 里 snapshot_eligible=True 的条目动手；
    - 应用前先算当前 sha256，与 expected_current_hash 不符就打成 conflict；
    - 应用后必须写一份 checkpoint_type="restore" 的 Checkpoint Record。
"""

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import secrets
import stat

from pico import security as securitylib
from pico.checkpoint_store import CheckpointStoreError
from pico.recovery_checkpoint_writer import coalesce_file_entries, validate_file_entry
from pico.recovery_policy import DEFAULT_MAX_BLOB_SIZE, snapshot_bytes_eligibility
from pico.recovery_models import (
    new_id,
    utc_now,
)
from pico.recovery_paths import (
    hash_bytes,
    normalize_workspace_relative_path,
    resolve_workspace_relative_path_no_symlinks,
)
from pico.tool_change_recorder import ToolChangeRecorder


_TERMINAL_TOOL_STATUSES = {
    "finalized",
    "error",
    "partial_success",
    "interrupted",
}
RECOVERY_REVIEW_KEYS = (
    "tool_changes",
    "restore_journals",
    "invalid_records",
    "quarantined_records",
)


def collect_recovery_review_items(store, workspace_root):
    del workspace_root
    tool_changes = []
    restore_journals = []
    invalid_records = []
    for item in store.list_tool_change_records(strict=False):
        if item.get("status") == "invalid_record":
            invalid_records.append(dict(item))
        elif item.get("status") == "pending" or (
            item.get("status") == "interrupted"
            and not item.get("reviewed_at")
        ):
            tool_changes.append(
                {
                    "tool_change_id": item["tool_change_id"],
                    "status": item["status"],
                    "owner_id": item.get("owner_id", ""),
                    "tool_name": item.get("tool_name", ""),
                    "effect_class": item.get("effect_class", ""),
                    "started_at": item.get("started_at", ""),
                }
            )
    for item in store.list_checkpoint_records(strict=False):
        if item.get("status") == "invalid_record":
            invalid_records.append(dict(item))
        elif item.get("checkpoint_type") == "restore" and (
            item.get("status") == "applying"
            or (item.get("status") == "partial" and not item.get("reviewed_at"))
        ):
            restore_journals.append(
                {
                    "checkpoint_id": item["checkpoint_id"],
                    "status": item["status"],
                    "owner_id": item.get("owner_id", ""),
                    "created_at": item.get("created_at", ""),
                }
            )
    return {
        "tool_changes": tool_changes,
        "restore_journals": restore_journals,
        "invalid_records": invalid_records,
        "quarantined_records": store.list_quarantined_records(),
    }


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


class RecoveryManager:
    def __init__(self, store, workspace_root, checkpoint_writer=None):
        self.store = store
        self.workspace_root = Path(os.path.abspath(os.fspath(workspace_root)))
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.workspace_root, flags)
        try:
            opened = os.fstat(descriptor)
            self._workspace_identity = (opened.st_dev, opened.st_ino)
        finally:
            os.close(descriptor)
        self._checkpoint_writer = checkpoint_writer
        self.owner_id = new_id("recovery")

    # 允许 Pico 在构造完成之后再把 writer 注入进来，避免循环依赖
    def bind_checkpoint_writer(self, writer):
        self._checkpoint_writer = writer

    def pending_restore_reviews(self):
        return [
            record
            for record in self.store.list_checkpoint_records(strict=True)
            if record.get("checkpoint_type") == "restore"
            and (
                record.get("status") == "applying"
                or (
                    record.get("status") == "partial"
                    and not record.get("reviewed_at")
                )
            )
        ]

    def _observe_restore_state(self, path):
        current = securitylib.read_regular_bytes_anchored(
            self.workspace_root,
            path,
            max_bytes=DEFAULT_MAX_BLOB_SIZE,
            expected_root_identity=self._workspace_identity,
        )
        data = current["data"]
        return {
            "exists": current["exists"],
            "hash": hash_bytes(data)["content_hash"] if data is not None else "",
            "mode": current["mode"],
        }, data

    def preview_restore_journal_resolution(self, checkpoint_id):
        record, raw = self.store.load_checkpoint_record_snapshot(checkpoint_id)
        if record.get("checkpoint_type") != "restore":
            raise ValueError("invalid_restore_journal")
        self._require_journal_workspace(record)
        status = record.get("status")
        if status == "partial" and not record.get("reviewed_at"):
            preview_status = "partial_review_required"
        elif status == "applying":
            preview_status = "applying_review_required"
        else:
            preview_status = status
        entries = []
        for intent in record.get("restore_provenance", {}).get("entries", []):
            observed, _ = self._observe_restore_state(intent["path"])
            entries.append(
                {
                    "path": intent["path"],
                    "classification": _classify_observed_state(
                        observed,
                        intent["pre_state"],
                        intent["planned_post_state"],
                    ),
                    "observed_state": observed,
                }
            )
        return {
            "checkpoint_id": checkpoint_id,
            "status": preview_status,
            "record_hash": hashlib.sha256(raw).hexdigest(),
            "entries": entries,
        }

    def resolve_restore_journal(
        self,
        checkpoint_id,
        *,
        expected_record_hash,
        reviewed_by,
        review_reason,
    ):
        with self.store.mutation_lock():
            record, raw = self.store.load_checkpoint_record_snapshot(checkpoint_id)
            if hashlib.sha256(raw).hexdigest() != expected_record_hash:
                raise CheckpointStoreError("record_changed", "record changed")
            if record.get("checkpoint_type") != "restore":
                raise ValueError("invalid_restore_journal")
            self._require_journal_workspace(record)
            status = record.get("status")
            if status == "partial":
                if record.get("reviewed_at"):
                    return record

                def accept_partial(current):
                    current["reviewed_at"] = utc_now()
                    current["reviewed_by"] = str(reviewed_by)
                    current["review_reason"] = str(review_reason)
                    return current

                return self.store.update_checkpoint_record_if_hash(
                    checkpoint_id,
                    expected_record_hash,
                    accept_partial,
                    expected_status="partial",
                )
            if status != "applying":
                raise ValueError("invalid_restore_journal_status")
            classifications = []
            for intent in record.get("restore_provenance", {}).get("entries", []):
                observed, _ = self._observe_restore_state(intent["path"])
                classifications.append(
                    (
                        _classify_observed_state(
                            observed,
                            intent["pre_state"],
                            intent["planned_post_state"],
                        ),
                        observed,
                    )
                )

            def resolve(current):
                intents = current["restore_provenance"].get("entries", [])
                for intent, (classification, observed) in zip(
                    intents, classifications
                ):
                    if intent.get("outcome") != "pending":
                        continue
                    if classification == "applied_unconfirmed":
                        intent.update(
                            outcome="applied",
                            reason="",
                            target_modified=True,
                            actual_post_state=dict(intent["planned_post_state"]),
                        )
                    elif classification == "not_applied":
                        intent.update(
                            outcome="not_attempted",
                            reason="not_applied",
                            target_modified=False,
                            actual_post_state={},
                        )
                    else:
                        intent.update(
                            outcome="uncertain",
                            reason="manual_recovery_required",
                            target_modified=True,
                            actual_post_state={},
                        )
                current["reviewed_at"] = utc_now()
                current["reviewed_by"] = str(reviewed_by)
                current["review_reason"] = str(review_reason)
                return self._terminalize_restore_record(current)

            return self.store.update_checkpoint_record_if_hash(
                checkpoint_id,
                expected_record_hash,
                resolve,
                expected_status="applying",
            )

    def _require_journal_workspace(self, record):
        try:
            recorded = Path(record.get("workspace_root", "")).resolve(strict=True)
            live = self.workspace_root.resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise ValueError("workspace_mismatch") from exc
        if recorded != live:
            raise ValueError("workspace_mismatch")

    def preview_restore(self, checkpoint_id):
        def finish(entries):
            return {
                "restore_plan_id": new_id("plan"),
                "checkpoint_id": checkpoint_id,
                "created_at": utc_now(),
                "status": self._plan_status(
                    [entry["decision"] for entry in entries]
                ),
                "entries": entries,
            }

        try:
            record = self.store.load_checkpoint_record(checkpoint_id)
        except (OSError, ValueError) as exc:
            return finish(
                [
                    self._plan_issue(
                        "error", getattr(exc, "code", "invalid_checkpoint")
                    )
                ]
            )
        try:
            recorded_root = Path(record.get("workspace_root", "")).resolve(
                strict=True
            )
            live_root = self.workspace_root.resolve(strict=True)
        except (OSError, ValueError):
            return finish([self._plan_issue("error", "workspace_mismatch")])
        if recorded_root != live_root:
            return finish([self._plan_issue("error", "workspace_mismatch")])
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.workspace_root, flags)
            try:
                current_root = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            return finish([self._plan_issue("error", "workspace_mismatch")])
        if (current_root.st_dev, current_root.st_ino) != self._workspace_identity:
            return finish([self._plan_issue("error", "workspace_mismatch")])
        if record.get("integrity_errors") or record.get(
            "missing_tool_change_ids"
        ):
            return finish(
                [self._plan_issue("error", "incomplete_tool_change_history")]
            )

        entries = []
        raw_entries = list(record.get("file_entries", []) or [])
        checkpoint_type = record.get("checkpoint_type")
        if checkpoint_type == "turn":
            known_sources = set(record.get("tool_change_ids", []))
            loaded_source_entries = []
            for tool_change_id in record.get("tool_change_ids", []):
                try:
                    source = self.store.load_tool_change_record(tool_change_id)
                except (OSError, ValueError):
                    return finish(
                        [self._plan_issue("error", "incomplete_tool_change_history")]
                    )
                if (
                    source.get("tool_change_id") != tool_change_id
                    or source.get("status") not in _TERMINAL_TOOL_STATUSES
                    or source.get("checkpoint_id") != checkpoint_id
                ):
                    return finish(
                        [self._plan_issue("error", "incomplete_tool_change_history")]
                    )
                loaded_source_entries.extend(source.get("file_entries", []) or [])
            if known_sources and coalesce_file_entries(loaded_source_entries) != raw_entries:
                return finish(
                    [self._plan_issue("error", "incomplete_tool_change_history")]
                )
            for item in raw_entries:
                if not isinstance(item, dict):
                    continue
                sources = item.get("source_tool_change_ids", [])
                if not isinstance(sources, list) or any(
                    not isinstance(source_id, str)
                    or source_id not in known_sources
                    for source_id in sources
                ):
                    return finish(
                        [self._plan_issue("error", "incomplete_tool_change_history")]
                    )
        elif checkpoint_type == "restore":
            status = record.get("status")
            if status == "partial" and not record.get("reviewed_at"):
                entries.append(
                    self._plan_issue("review", "partial_review_required")
                )
            elif status == "applying":
                return finish(
                    [self._plan_issue("review", "restore_applying_review_required")]
                )
            elif status in {"blocked", "failed", "noop"}:
                return finish([])
            elif status not in {"applied", "partial"}:
                return finish([self._plan_issue("error", "invalid_restore_status")])
        else:
            return finish([self._plan_issue("error", "invalid_checkpoint_type")])

        valid_entries = []
        counts = {}
        for file_entry in raw_entries:
            path = (
                file_entry.get("path", "")
                if isinstance(file_entry, dict)
                else ""
            )
            if isinstance(file_entry, dict):
                try:
                    resolve_workspace_relative_path_no_symlinks(
                        self.workspace_root, path
                    )
                except ValueError as exc:
                    reason = str(exc)
                    if reason not in {"symlink", "missing_parent"}:
                        reason = "invalid_path"
                    entries.append(
                        self._plan_issue("error", reason, path=path)
                    )
                    continue
            strict_reason = validate_file_entry(file_entry)
            if strict_reason:
                entries.append(
                    self._plan_issue(
                        "error",
                        strict_reason,
                        path=path,
                    )
                )
                continue
            path = file_entry["path"]
            if file_entry.get("ineligible_reason") == "mode_unknown" or (
                checkpoint_type == "turn"
                and known_sources
                and not file_entry.get("source_tool_change_ids")
            ):
                entries.append(
                    self._plan_issue(
                        "review",
                        file_entry.get("ineligible_reason") or "incomplete_tool_change_history",
                        file_entry=file_entry,
                    )
                )
                continue
            counts[path] = counts.get(path, 0) + 1
            valid_entries.append(file_entry)
        ambiguous_paths = {
            path
            for path, count in counts.items()
            if count > 1
            and any(
                item["path"] == path and not item["source_tool_change_ids"]
                for item in valid_entries
            )
        }
        if ambiguous_paths:
            valid_entries = [
                item
                for item in valid_entries
                if item["path"] not in ambiguous_paths
            ]
            entries.extend(
                self._plan_issue("review", "legacy_ambiguous_history", path=path)
                for path in sorted(ambiguous_paths)
            )
        for file_entry in coalesce_file_entries(valid_entries):
            decision, detail = self._plan_entry(file_entry)
            entries.append(
                self._plan_issue(
                    decision,
                    detail.get("reason", ""),
                    file_entry=file_entry,
                    observed_current_hash=detail.get(
                        "observed_current_hash", ""
                    ),
                    recovery_note=detail.get("recovery_note", ""),
                )
            )
        return finish(entries)

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

    def _plan_issue(
        self,
        decision,
        reason,
        *,
        path="",
        file_entry=None,
        observed_current_hash="",
        recovery_note="",
    ):
        file_entry = file_entry or {}
        return {
            "path": path or file_entry.get("path", ""),
            "decision": decision,
            "reason": reason,
            "restore_available": decision == "restore",
            "captured_before_state": bool(file_entry.get("before_blob_ref", "")),
            "recovery_note": recovery_note,
            "expected_current_hash": file_entry.get("expected_current_hash", ""),
            "observed_current_hash": observed_current_hash,
            "before_blob_ref": file_entry.get("before_blob_ref", ""),
            "before_hash": file_entry.get("before_hash", ""),
            "after_blob_ref": file_entry.get("after_blob_ref", ""),
            "after_hash": file_entry.get("after_hash", ""),
            "before_exists": file_entry.get("before_exists"),
            "before_mode": file_entry.get("before_mode"),
            "after_exists": file_entry.get("after_exists"),
            "after_mode": file_entry.get("after_mode"),
            "snapshot_eligible": bool(file_entry.get("snapshot_eligible", False)),
            "ineligible_reason": file_entry.get("ineligible_reason", ""),
            "change_kind": file_entry.get("change_kind", ""),
            "source_tool_change_ids": list(
                file_entry.get("source_tool_change_ids", []) or []
            ),
        }

    def _plan_entry(self, file_entry):
        if not file_entry.get("snapshot_eligible", False):
            reason = file_entry.get("ineligible_reason", "not_snapshot_eligible")
            if file_entry.get("before_blob_ref", ""):
                note = f"review required: {reason}; automatic restore is disabled for this entry"
            else:
                note = f"review required: {reason}; no restorable before-state snapshot was captured"
            return "review", {"reason": reason, "recovery_note": note}
        change_kind = file_entry.get("change_kind", "")
        if change_kind in {"modified", "deleted"} and not file_entry.get("before_blob_ref", ""):
            return "review", {"reason": "before_blob_unavailable", "observed_current_hash": ""}
        path = file_entry.get("path", "")
        if securitylib.is_sensitive_path(path):
            return "review", {"reason": "sensitive_path", "observed_current_hash": ""}
        for prefix in ("before", "after"):
            if not file_entry.get(prefix + "_exists"):
                continue
            try:
                blob_data = self.store.read_blob(
                    file_entry.get(prefix + "_blob_ref", "")
                )
            except FileNotFoundError:
                return "error", {
                    "reason": prefix + "_blob_missing",
                    "observed_current_hash": "",
                }
            except (OSError, ValueError) as exc:
                reason = (
                    prefix + "_blob_hash_mismatch"
                    if str(exc) == "blob_hash_mismatch"
                    else prefix + "_blob_invalid"
                )
                return "error", {
                    "reason": reason,
                    "observed_current_hash": "",
                }
            if hash_bytes(blob_data)["content_hash"] != file_entry.get(
                prefix + "_hash"
            ):
                return "error", {
                    "reason": prefix + "_blob_hash_mismatch",
                    "observed_current_hash": "",
                }
            eligibility = snapshot_bytes_eligibility(
                self.workspace_root,
                path,
                blob_data,
                max_blob_size=DEFAULT_MAX_BLOB_SIZE,
            )
            if not eligibility["snapshot_eligible"]:
                return "review", {
                    "reason": eligibility["ineligible_reason"],
                    "observed_current_hash": "",
                }
        try:
            current = securitylib.read_regular_bytes_anchored(
                self.workspace_root,
                path,
                max_bytes=DEFAULT_MAX_BLOB_SIZE,
            )
        except (OSError, ValueError):
            return "error", {"reason": "read_failed", "observed_current_hash": ""}

        expected = file_entry.get("expected_current_hash", "")
        observed = ""
        current_data = current["data"]
        if current["exists"]:
            eligibility = snapshot_bytes_eligibility(
                self.workspace_root,
                path,
                current_data,
                max_blob_size=DEFAULT_MAX_BLOB_SIZE,
            )
            if not eligibility["snapshot_eligible"]:
                return "review", {
                    "reason": eligibility["ineligible_reason"],
                    "observed_current_hash": "",
                }
            observed = hash_bytes(current_data)["content_hash"]

        if current["exists"] != file_entry.get("after_exists"):
            reason = "unexpected_file_present" if current["exists"] else "file_missing"
            return "conflict", {"reason": reason, "observed_current_hash": observed}
        if expected != observed:
            return "conflict", {"reason": "hash_mismatch", "observed_current_hash": observed}
        if current["mode"] != file_entry.get("after_mode"):
            return "conflict", {
                "reason": "mode_mismatch",
                "observed_current_hash": observed,
            }
        return "restore", {"reason": "hash_match", "observed_current_hash": observed}

    def apply_restore(self, checkpoint_id, *, operation_id="", plan_digest=""):
        with self.store.mutation_lock():
            try:
                tool_reviews = ToolChangeRecorder(
                    self.store, owner_id="recovery-guard"
                ).pending_recovery_reviews()
                restore_reviews = self.pending_restore_reviews()
            except (CheckpointStoreError, OSError, ValueError):
                return self._write_recovery_review_blocked_audit(checkpoint_id)
            if tool_reviews or restore_reviews:
                return self._write_recovery_review_blocked_audit(checkpoint_id)
            plan = self._preview_restore_locked(checkpoint_id)
            plan["operation_id"] = str(operation_id or "")
            plan["rewind_plan_digest"] = str(plan_digest or "")
            if plan["status"] in {"invalid", "conflicted", "review_required"}:
                return self._write_non_mutating_restore_audit(
                    plan, status="blocked"
                )
            if plan["status"] == "noop":
                return self._write_non_mutating_restore_audit(plan, status="noop")
            capture = self._capture_restore_intents(plan)
            if capture["status"] == "blocked":
                return self._write_non_mutating_restore_audit(
                    plan, status="blocked", reason=capture["reason"]
                )
            journal = self._write_applying_journal(plan, capture["intents"])
            self._apply_all_intents(
                journal["checkpoint_id"],
                journal["restore_provenance"]["entries"],
            )
            return self._finish_restore_journal(journal["checkpoint_id"])

    def _preview_restore_locked(self, checkpoint_id):
        return self.preview_restore(checkpoint_id)

    def _capture_restore_intents(self, plan):
        intents = []
        for entry in plan["entries"]:
            if entry["decision"] != "restore":
                continue
            path = entry["path"]
            try:
                captured_data, parent_identities = _read_workspace_bytes_no_follow(
                    self.workspace_root,
                    path,
                    expected_root_identity=self._workspace_identity,
                    return_parent_identities=True,
                )
                current = securitylib.read_regular_bytes_anchored(
                    self.workspace_root,
                    path,
                    max_bytes=DEFAULT_MAX_BLOB_SIZE,
                )
            except (OSError, ValueError, _RestoreInputError):
                return {"status": "blocked", "reason": "read_failed", "intents": []}
            data = current["data"]
            if data != captured_data:
                return {
                    "status": "blocked",
                    "reason": "current_state_changed",
                    "intents": [],
                }
            observed_hash = hash_bytes(data)["content_hash"] if data is not None else ""
            if (
                current["exists"] != entry["after_exists"]
                or observed_hash != entry["after_hash"]
                or current["mode"] != entry["after_mode"]
            ):
                return {
                    "status": "blocked",
                    "reason": "current_state_changed",
                    "intents": [],
                }
            if data is not None:
                eligibility = snapshot_bytes_eligibility(
                    self.workspace_root,
                    path,
                    data,
                    max_blob_size=DEFAULT_MAX_BLOB_SIZE,
                )
                if not eligibility["snapshot_eligible"]:
                    return {
                        "status": "blocked",
                        "reason": eligibility["ineligible_reason"],
                        "intents": [],
                    }
                pre_blob_ref = self.store.write_blob(data, "text")["blob_ref"]
            else:
                pre_blob_ref = ""
            try:
                live_parents = _live_parent_identities(
                    self.workspace_root,
                    Path(normalize_workspace_relative_path(path)).parts,
                    self._workspace_identity,
                )
            except (OSError, ValueError, _RestoreInputError):
                live_parents = ()
            if live_parents != tuple(parent_identities):
                return {
                    "status": "blocked",
                    "reason": "current_state_changed",
                    "intents": [],
                }
            intents.append(
                {
                    "path": path,
                    "pre_state": {
                        "exists": current["exists"],
                        "hash": observed_hash,
                        "blob_ref": pre_blob_ref,
                        "mode": current["mode"],
                    },
                    "planned_post_state": {
                        "exists": entry["before_exists"],
                        "hash": entry["before_hash"],
                        "blob_ref": entry["before_blob_ref"],
                        "mode": entry["before_mode"],
                    },
                    "outcome": "pending",
                    "reason": "",
                    "target_modified": False,
                    "actual_post_state": {},
                }
            )
        return {"status": "ready", "reason": "", "intents": intents}

    def _writer(self):
        return self._checkpoint_writer or RecoveryCheckpointWriterProxy(
            self.store, self.workspace_root
        )

    def _source_metadata(self, checkpoint_id):
        try:
            return self.store.load_checkpoint_record(checkpoint_id)
        except (OSError, ValueError):
            return {}

    def _write_non_mutating_restore_audit(self, plan, *, status, reason=""):
        checkpoint_id = plan["checkpoint_id"]
        source = self._source_metadata(checkpoint_id)
        provenance = {
            "source_checkpoint_id": checkpoint_id,
            "plan_id": plan.get("restore_plan_id", ""),
            "operation_id": plan.get("operation_id", ""),
            "rewind_plan_digest": plan.get("rewind_plan_digest", ""),
            "reason": reason,
            "entries": [],
            "restored_paths": [],
            "skipped_entries": [
                {"path": item.get("path", ""), "reason": item.get("reason", "")}
                for item in plan.get("entries", [])
                if item.get("decision") != "restore"
            ],
        }
        audit = self._writer().create_restore_checkpoint(
            session_id=source.get("session_id", ""),
            run_id=source.get("run_id", ""),
            turn_id=source.get("turn_id", ""),
            parent_checkpoint_id=checkpoint_id,
            restore_provenance=provenance,
            status=status,
            owner_id=self.owner_id,
        )
        return {
            "status": status,
            "restore_checkpoint_id": audit["checkpoint_id"],
            "restore_plan_id": plan.get("restore_plan_id", ""),
            "restored_paths": [],
            "skipped_entries": provenance["skipped_entries"],
        }

    def _write_recovery_review_blocked_audit(self, checkpoint_id):
        return self._write_non_mutating_restore_audit(
            {
                "checkpoint_id": checkpoint_id,
                "restore_plan_id": new_id("plan"),
                "entries": [
                    self._plan_issue("review", "recovery_review_required")
                ],
            },
            status="blocked",
            reason="recovery_review_required",
        )

    def _write_applying_journal(self, plan, intents):
        checkpoint_id = plan["checkpoint_id"]
        source = self.store.load_checkpoint_record(checkpoint_id)
        return self._writer().create_restore_checkpoint(
            session_id=source.get("session_id", ""),
            run_id=source.get("run_id", ""),
            turn_id=source.get("turn_id", ""),
            parent_checkpoint_id=checkpoint_id,
            restore_provenance={
                "source_checkpoint_id": checkpoint_id,
                "plan_id": plan["restore_plan_id"],
                "operation_id": plan.get("operation_id", ""),
                "rewind_plan_digest": plan.get("rewind_plan_digest", ""),
                "entries": intents,
                "restored_paths": [],
                "skipped_entries": [],
            },
            status="applying",
            owner_id=self.owner_id,
        )

    def _apply_all_intents(self, restore_checkpoint_id, intents):
        for index, intent in enumerate(intents):
            try:
                outcome = self._apply_intent(restore_checkpoint_id, intent)
            except RestoreMutationError as exc:
                if exc.code in {
                    "mutation_durability_unknown",
                    "delete_rollback_failed",
                }:
                    raise
                outcome = self._outcome_after_mutation_error(intent, exc)
            except Exception as exc:
                outcome = {
                    "outcome": "failed",
                    "reason": str(exc) or "restore_failed",
                    "target_modified": False,
                    "actual_post_state": {},
                }
            if outcome.get("target_modified") and outcome.get("outcome") == "failed":
                outcome = self._outcome_after_mutation_error(
                    intent,
                    RestoreMutationError(
                        outcome.get("reason", "post_state_mismatch"),
                        target_modified=True,
                    ),
                )
            if outcome.get("outcome") != "applied":

                def fail_and_stop(record):
                    journal_entries = record["restore_provenance"]["entries"]
                    journal_entries[index].update(outcome)
                    for tail in journal_entries[index + 1 :]:
                        tail.update(
                            outcome="not_attempted",
                            reason="prior_intent_failed",
                            target_modified=False,
                            actual_post_state={},
                        )
                    return record

                self.store.update_checkpoint_record(
                    restore_checkpoint_id,
                    fail_and_stop,
                    expected_status="applying",
                )
                return
            self._write_intent_outcome(restore_checkpoint_id, index, outcome)

    def _write_intent_outcome(self, restore_checkpoint_id, index, outcome):
        def update(record):
            record["restore_provenance"]["entries"][index].update(outcome)
            return record

        return self.store.update_checkpoint_record(
            restore_checkpoint_id, update, expected_status="applying"
        )

    def _outcome_after_mutation_error(self, intent, exc):
        if not exc.target_modified:
            return {
                "outcome": "failed",
                "reason": exc.code,
                "target_modified": False,
                "actual_post_state": {},
            }
        try:
            observed, data = self._observe_restore_state(intent["path"])
            if data is not None:
                eligibility = snapshot_bytes_eligibility(
                    self.workspace_root,
                    intent["path"],
                    data,
                    max_blob_size=DEFAULT_MAX_BLOB_SIZE,
                )
                if not eligibility["snapshot_eligible"]:
                    raise ValueError("ineligible_actual_post")
                observed["blob_ref"] = self.store.write_blob(data, "text")[
                    "blob_ref"
                ]
            else:
                observed["blob_ref"] = ""
        except (OSError, ValueError, _RestoreInputError):
            return {
                "outcome": "uncertain",
                "reason": "manual_recovery_required",
                "target_modified": True,
                "actual_post_state": {},
            }
        classification = _classify_observed_state(
            observed, intent["pre_state"], intent["planned_post_state"]
        )
        if classification == "applied_unconfirmed":
            return {
                "outcome": "applied",
                "reason": "",
                "target_modified": True,
                "actual_post_state": dict(intent["planned_post_state"]),
            }
        if classification == "not_applied":
            return {
                "outcome": "failed",
                "reason": exc.code,
                "target_modified": True,
                "actual_post_state": observed,
            }
        return {
            "outcome": "uncertain",
            "reason": "manual_recovery_required",
            "target_modified": True,
            "actual_post_state": observed,
        }

    def _apply_intent(self, restore_checkpoint_id, intent):
        del restore_checkpoint_id
        path = intent["path"]
        pre = intent["pre_state"]
        post = intent["planned_post_state"]
        try:
            current_data, parent_identities = _read_workspace_bytes_no_follow(
                self.workspace_root,
                path,
                expected_root_identity=self._workspace_identity,
                return_parent_identities=True,
            )
        except (ValueError, _RestoreInputError):
            return {"outcome": "failed", "reason": "read_failed"}
        current = securitylib.read_regular_bytes_anchored(
            self.workspace_root, path, max_bytes=DEFAULT_MAX_BLOB_SIZE
        )
        current_hash = (
            hash_bytes(current_data)["content_hash"]
            if current_data is not None
            else ""
        )
        if (
            current["exists"] != pre["exists"]
            or current_hash != pre["hash"]
            or current["mode"] != pre["mode"]
        ):
            return {"outcome": "failed", "reason": "current_state_changed"}
        replacement = (
            self.store.read_blob(post["blob_ref"]) if post["exists"] else None
        )
        result = _mutate_workspace_bytes_if_unchanged(
            self.workspace_root,
            path,
            expected_data=current_data,
            expected_mode=pre["mode"],
            replacement_data=replacement,
            replacement_mode=post["mode"],
            expected_root_identity=self._workspace_identity,
            expected_parent_identities=parent_identities,
        )
        if result["status"] != "ok":
            return {
                "outcome": "failed",
                "reason": result["reason"],
                "target_modified": bool(result.get("target_modified", False)),
            }
        try:
            self._fsync_target_parent(self.workspace_root / path)
            actual = securitylib.read_regular_bytes_anchored(
                self.workspace_root, path, max_bytes=DEFAULT_MAX_BLOB_SIZE
            )
        except (OSError, ValueError) as exc:
            raise RestoreMutationError(
                "post_mutation_verification_failed", target_modified=True
            ) from exc
        actual_hash = (
            hash_bytes(actual["data"])["content_hash"]
            if actual["data"] is not None
            else ""
        )
        actual_state = {
            "exists": actual["exists"],
            "hash": actual_hash,
            "blob_ref": post["blob_ref"] if actual["exists"] else "",
            "mode": actual["mode"],
        }
        if actual_state != post:
            return {
                "outcome": "failed",
                "reason": "post_state_mismatch",
                "target_modified": True,
                "actual_post_state": actual_state,
            }
        return {
            "outcome": "applied",
            "reason": "",
            "target_modified": True,
            "actual_post_state": actual_state,
        }

    def _fsync_target_parent(self, path):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(Path(path).parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _finish_restore_journal(self, restore_checkpoint_id):
        def finish(record):
            return self._terminalize_restore_record(record)

        journal = self.store.update_checkpoint_record(
            restore_checkpoint_id, finish, expected_status="applying"
        )
        provenance = journal["restore_provenance"]
        return {
            "status": journal["status"],
            "restore_checkpoint_id": restore_checkpoint_id,
            "restore_plan_id": provenance.get("plan_id", ""),
            "restored_paths": provenance.get("restored_paths", []),
            "skipped_entries": provenance.get("skipped_entries", []),
        }

    def _terminalize_restore_record(self, record):
        entries = record["restore_provenance"].get("entries", [])
        applied = [item for item in entries if item.get("outcome") == "applied"]
        uncertain = [
            item for item in entries if item.get("outcome") == "uncertain"
        ]
        if entries and len(applied) == len(entries):
            status = "applied"
        elif applied or uncertain:
            status = "partial"
        else:
            status = "failed"
        record["status"] = status
        record["restore_provenance"]["restored_paths"] = [
            item["path"] for item in applied
        ]
        record["restore_provenance"]["skipped_entries"] = [
            {"path": item["path"], "reason": item.get("reason", "")}
            for item in entries
            if item.get("outcome") != "applied"
        ]
        record["file_entries"] = [
            file_entry
            for file_entry in (
                self._file_entry_for_applied_intent(item) for item in applied
            )
            if file_entry is not None
        ]
        return record

    def _file_entry_for_applied_intent(self, intent):
        before = intent["pre_state"]
        after = intent.get("actual_post_state") or intent["planned_post_state"]
        if not before["exists"] and not after["exists"]:
            return None
        if (
            before["exists"]
            and after["exists"]
            and before["hash"] == after["hash"]
            and before["mode"] == after["mode"]
        ):
            return None
        return {
            "path": intent["path"],
            "change_kind": (
                "created"
                if not before["exists"] and after["exists"]
                else "deleted"
                if before["exists"] and not after["exists"]
                else "modified"
            ),
            "snapshot_eligible": True,
            "ineligible_reason": "",
            "before_exists": before["exists"],
            "before_blob_ref": before["blob_ref"],
            "before_hash": before["hash"],
            "before_mode": before["mode"],
            "after_exists": after["exists"],
            "after_blob_ref": after["blob_ref"],
            "after_hash": after["hash"],
            "after_mode": after["mode"],
            "expected_current_hash": after["hash"] if after["exists"] else "",
            "source_tool_change_ids": [],
        }


class RecoveryCheckpointWriterProxy:
    """在没有真实 writer 注入的情况下，走同样的 store 写法。"""

    def __init__(self, store, workspace_root):
        from pico.recovery_checkpoint_writer import RecoveryCheckpointWriter

        self._writer = RecoveryCheckpointWriter(store, workspace_root)

    def create_restore_checkpoint(self, **kwargs):
        return self._writer.create_restore_checkpoint(**kwargs)


class _RestoreInputError(Exception):
    """Stable internal marker for an unsafe or unreadable restore input."""


class RestoreMutationError(OSError):
    def __init__(self, code, *, target_modified):
        super().__init__(code)
        self.code = str(code)
        self.target_modified = bool(target_modified)


_RENAME_SWAP = 0x00000002
_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 0x00000001
try:
    _RENAMEATX_NP = ctypes.CDLL(None, use_errno=True).renameatx_np
    _RENAMEATX_NP.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEATX_NP.restype = ctypes.c_int
except AttributeError:
    _RENAMEATX_NP = None

try:
    _RENAMEAT2 = ctypes.CDLL(None, use_errno=True).renameat2
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int
except AttributeError:
    _RENAMEAT2 = None


def _rename_swap(
    parent_descriptor,
    first,
    second,
    *,
    second_parent_descriptor=None,
):
    rename = _RENAMEATX_NP or _RENAMEAT2
    if rename is None:
        raise OSError(errno.ENOTSUP, "atomic restore swap unavailable")
    second_parent_descriptor = (
        parent_descriptor
        if second_parent_descriptor is None
        else second_parent_descriptor
    )
    ctypes.set_errno(0)
    result = rename(
        parent_descriptor,
        os.fsencode(first),
        second_parent_descriptor,
        os.fsencode(second),
        _RENAME_SWAP,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _rename_noreplace(
    source_parent_descriptor,
    source_name,
    destination_parent_descriptor,
    destination_name,
):
    if _RENAMEATX_NP is not None:
        rename = _RENAMEATX_NP
        flags = _RENAME_EXCL
    elif _RENAMEAT2 is not None:
        rename = _RENAMEAT2
        flags = _RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename unavailable")
    ctypes.set_errno(0)
    result = rename(
        source_parent_descriptor,
        os.fsencode(source_name),
        destination_parent_descriptor,
        os.fsencode(destination_name),
        flags,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _read_workspace_bytes_no_follow(
    workspace_root,
    raw_path,
    *,
    max_bytes=DEFAULT_MAX_BLOB_SIZE,
    expected_root_identity=None,
    return_parent_identities=False,
):
    """Read one regular workspace file through an anchored, bounded descriptor chain."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory or os.open not in getattr(os, "supports_dir_fd", ()):
        raise _RestoreInputError("read_failed")
    normalized = normalize_workspace_relative_path(raw_path)
    parts = Path(normalized).parts
    if not parts:
        raise _RestoreInputError("read_failed")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    directory_flags = flags | directory
    root = Path(os.path.abspath(os.fspath(workspace_root)))
    descriptors = []
    try:
        descriptors.append(os.open(root, directory_flags))
        opened_root = os.fstat(descriptors[0])
        if expected_root_identity is not None and (
            opened_root.st_dev,
            opened_root.st_ino,
        ) != tuple(expected_root_identity):
            raise _RestoreInputError("read_failed")
        for part in parts[:-1]:
            descriptors.append(
                os.open(part, directory_flags, dir_fd=descriptors[-1])
            )
        try:
            leaf = os.open(
                parts[-1],
                flags | getattr(os, "O_NONBLOCK", 0),
                dir_fd=descriptors[-1],
            )
        except FileNotFoundError:
            result = None
            if return_parent_identities:
                return result, tuple(
                    (os.fstat(item).st_dev, os.fstat(item).st_ino)
                    for item in descriptors
                )
            return result
        try:
            info = os.fstat(leaf)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _RestoreInputError("read_failed")
            chunks = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(leaf, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise _RestoreInputError("file_too_large")
            if return_parent_identities:
                return data, tuple(
                    (os.fstat(item).st_dev, os.fstat(item).st_ino)
                    for item in descriptors
                )
            return data
        finally:
            os.close(leaf)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _RestoreInputError("read_failed") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_restore_leaf(parent_descriptor, name, *, max_bytes=DEFAULT_MAX_BLOB_SIZE):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise _RestoreInputError("read_failed")
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise _RestoreInputError("file_too_large")
        return data
    finally:
        os.close(descriptor)


def _write_restore_bytes(descriptor, data):
    written_total = 0
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("restore write failed")
        written_total += written
        view = view[written:]
    return written_total


def _live_parent_identities(workspace_root, parts, expected_root_identity):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors = []
    try:
        root = Path(os.path.abspath(os.fspath(workspace_root)))
        descriptors.append(os.open(root, flags))
        root_info = os.fstat(descriptors[0])
        if (root_info.st_dev, root_info.st_ino) != tuple(expected_root_identity):
            raise _RestoreInputError("read_failed")
        for part in parts[:-1]:
            descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
        return tuple(
            (os.fstat(item).st_dev, os.fstat(item).st_ino)
            for item in descriptors
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _mutate_workspace_bytes_if_unchanged(
    workspace_root,
    raw_path,
    *,
    expected_data,
    expected_mode=None,
    replacement_data,
    replacement_mode=None,
    expected_root_identity,
    expected_parent_identities,
):
    """Compare and mutate one workspace leaf through the same anchored parent fd."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory or os.open not in getattr(os, "supports_dir_fd", ()):
        return {"status": "error", "reason": "read_failed", "observed_hash": ""}
    normalized = normalize_workspace_relative_path(raw_path)
    parts = Path(normalized).parts
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow | directory
    )
    descriptors = []
    temp_name = ""
    temp_identity = None
    target_modified = False
    try:
        root = Path(os.path.abspath(os.fspath(workspace_root)))
        descriptors.append(os.open(root, directory_flags))
        opened_root = os.fstat(descriptors[0])
        if (opened_root.st_dev, opened_root.st_ino) != tuple(expected_root_identity):
            return {"status": "error", "reason": "read_failed", "observed_hash": ""}
        for part in parts[:-1]:
            try:
                child = os.open(part, directory_flags, dir_fd=descriptors[-1])
            except FileNotFoundError:
                if expected_data is not None:
                    return {"status": "error", "reason": "current_state_changed", "observed_hash": ""}
                os.mkdir(part, 0o755, dir_fd=descriptors[-1])
                child = os.open(part, directory_flags, dir_fd=descriptors[-1])
            descriptors.append(child)
        opened_parent_identities = tuple(
            (os.fstat(item).st_dev, os.fstat(item).st_ino)
            for item in descriptors
        )
        if opened_parent_identities != tuple(expected_parent_identities):
            return {"status": "error", "reason": "current_state_changed", "observed_hash": ""}
        parent = descriptors[-1]
        current = _read_restore_leaf(parent, parts[-1])
        if current != expected_data:
            return {
                "status": "error",
                "reason": "current_state_changed",
                "observed_hash": hash_bytes(current)["content_hash"] if current is not None else "",
            }
        current_mode = None
        if current is not None:
            current_info = os.stat(
                parts[-1], dir_fd=parent, follow_symlinks=False
            )
            current_mode = stat.S_IMODE(current_info.st_mode)
        if current_mode != expected_mode:
            return {
                "status": "error",
                "reason": "current_state_changed",
                "observed_hash": (
                    hash_bytes(current)["content_hash"]
                    if current is not None
                    else ""
                ),
            }
        if current is not None:
            eligibility = snapshot_bytes_eligibility(
                workspace_root,
                normalized,
                current,
                max_blob_size=DEFAULT_MAX_BLOB_SIZE,
            )
            if not eligibility["snapshot_eligible"]:
                return {"status": "error", "reason": eligibility["ineligible_reason"], "observed_hash": ""}
        if replacement_data is None:
            if current is not None:
                moved_name = f".{parts[-1]}.restore-delete.{secrets.token_hex(12)}.tmp"
                os.rename(
                    parts[-1],
                    moved_name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
                target_modified = True
                moved_data = _read_restore_leaf(parent, moved_name)
                if moved_data != expected_data:
                    try:
                        os.link(
                            moved_name,
                            parts[-1],
                            src_dir_fd=parent,
                            dst_dir_fd=parent,
                            follow_symlinks=False,
                        )
                        os.unlink(moved_name, dir_fd=parent)
                        os.fsync(parent)
                        if _read_restore_leaf(parent, parts[-1]) == expected_data:
                            target_modified = False
                    except FileExistsError:
                        pass
                    if target_modified:
                        raise RestoreMutationError(
                            "delete_rollback_failed", target_modified=True
                        )
                    return {"status": "error", "reason": "current_state_changed", "observed_hash": ""}
                if _read_restore_leaf(parent, parts[-1]) is not None:
                    raise RestoreMutationError(
                        "delete_target_reappeared", target_modified=True
                    )
                os.unlink(moved_name, dir_fd=parent)
            os.fsync(parent)
            if _live_parent_identities(
                workspace_root, parts, expected_root_identity
            ) != tuple(expected_parent_identities):
                return {"status": "error", "reason": "current_state_changed", "observed_hash": ""}
            return {"status": "ok", "reason": "", "observed_hash": ""}

        temp_name = f".{parts[-1]}.restore.{secrets.token_hex(12)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | nofollow
        descriptor = os.open(temp_name, flags, 0o600, dir_fd=parent)
        try:
            temp_info = os.fstat(descriptor)
            temp_identity = (temp_info.st_dev, temp_info.st_ino)
            _write_restore_bytes(descriptor, replacement_data)
            if replacement_mode is not None:
                os.fchmod(descriptor, replacement_mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temp_data = _read_restore_leaf(parent, temp_name)
        if temp_data != replacement_data:
            return {"status": "error", "reason": "post_write_hash_mismatch", "observed_hash": hash_bytes(temp_data or b"")["content_hash"]}
        if _read_restore_leaf(parent, parts[-1]) != expected_data:
            return {"status": "error", "reason": "current_state_changed", "observed_hash": ""}
        if expected_data is None:
            try:
                os.link(
                    temp_name,
                    parts[-1],
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileExistsError:
                return {"status": "error", "reason": "current_state_changed", "observed_hash": ""}
            target_modified = True
            os.unlink(temp_name, dir_fd=parent)
            temp_name = ""
        else:
            _rename_swap(parent, temp_name, parts[-1])
            target_modified = True
            swapped_out = _read_restore_leaf(parent, temp_name)
            if swapped_out != expected_data:
                _rename_swap(parent, temp_name, parts[-1])
                target_modified = False
                return {"status": "error", "reason": "current_state_changed", "observed_hash": ""}
            os.unlink(temp_name, dir_fd=parent)
            temp_name = ""
        observed = _read_restore_leaf(parent, parts[-1])
        observed_hash = hash_bytes(observed or b"")["content_hash"]
        expected_hash = hash_bytes(replacement_data)["content_hash"]
        os.fsync(parent)
        if observed != replacement_data:
            return {"status": "error", "reason": "reread_hash_mismatch_after_replace", "observed_hash": observed_hash, "target_modified": True}
        if _live_parent_identities(
            workspace_root, parts, expected_root_identity
        ) != tuple(expected_parent_identities):
            return {"status": "error", "reason": "current_state_changed", "observed_hash": ""}
        return {"status": "ok", "reason": "", "observed_hash": expected_hash}
    except (OSError, ValueError, _RestoreInputError) as exc:
        if target_modified:
            if isinstance(exc, RestoreMutationError):
                raise
            raise RestoreMutationError(
                "mutation_durability_unknown", target_modified=True
            ) from exc
        return {"status": "error", "reason": "read_failed", "observed_hash": ""}
    finally:
        if temp_name and descriptors:
            try:
                current = os.stat(temp_name, dir_fd=descriptors[-1], follow_symlinks=False)
                if temp_identity == (current.st_dev, current.st_ino):
                    os.unlink(temp_name, dir_fd=descriptors[-1])
            except OSError:
                pass
        for descriptor in reversed(descriptors):
            os.close(descriptor)
