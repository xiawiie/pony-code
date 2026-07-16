import pytest

from pico.state.checkpoint_store import CheckpointStore
from pico.recovery.checkpoint_writer import (
    RecoveryCheckpointWriter,
    coalesce_file_entries,
    validate_file_entry,
)
from pico.tools.change_recorder import ToolChangeRecorder
from pico.recovery.models import new_tool_change_record


def test_turn_checkpoint_links_real_tool_changes_and_file_entries(tmp_path):
    store = CheckpointStore(tmp_path)
    blob = store.write_blob(b"after\n", "text")
    tool_change = new_tool_change_record("tc_1", "", "task_1", "write_file", "workspace_write")
    tool_change["status"] = "finalized"
    tool_change["affected_paths"] = ["note.txt"]
    tool_change["file_entries"] = [
        _state_entry(
            "note.txt",
            (False, "", None),
            (True, blob["content_hash"], 0o644),
            "tc_1",
        )
    ]
    store.write_tool_change_record(tool_change)

    writer = RecoveryCheckpointWriter(store, tmp_path)
    checkpoint = writer.create_turn_checkpoint(
        session_id="session_1",
        run_id="run_1",
        turn_id="task_1",
        parent_checkpoint_id="",
        tool_change_ids=["tc_1"],
        verification_evidence=[],
    )

    loaded = store.load_checkpoint_record(checkpoint["checkpoint_id"])
    assert loaded["tool_change_ids"] == ["tc_1"]
    assert loaded["file_entries"][0]["path"] == "note.txt"
    assert loaded["integrity_errors"] == []


def _state_entry(path, before, after, source_id):
    before_exists, before_hash, before_mode = before
    after_exists, after_hash, after_mode = after
    return {
        "path": path,
        "change_kind": (
            "created"
            if not before_exists and after_exists
            else "deleted"
            if before_exists and not after_exists
            else "modified"
        ),
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
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    result = coalesce_file_entries(
        [
            _state_entry("note.txt", (True, a, 0o644), (True, b, 0o644), "tc_1"),
            _state_entry("note.txt", (True, b, 0o644), (True, c, 0o600), "tc_2"),
        ]
    )
    assert len(result) == 1
    assert result[0]["before_hash"] == a
    assert result[0]["after_hash"] == c
    assert result[0]["after_mode"] == 0o600
    assert result[0]["source_tool_change_ids"] == ["tc_1", "tc_2"]


def test_present_blank_hash_cannot_prove_continuity():
    c = "c" * 64
    result = coalesce_file_entries(
        [
            _state_entry("note.txt", (True, "", 0o644), (True, "", 0o644), "tc_1"),
            _state_entry("note.txt", (True, "", 0o644), (True, c, 0o644), "tc_2"),
        ]
    )
    assert result[0]["snapshot_eligible"] is False
    assert result[0]["ineligible_reason"] == "discontinuous_history"


def test_same_hash_different_mode_is_not_noop():
    value = "d" * 64
    result = coalesce_file_entries(
        [_state_entry("run.sh", (True, value, 0o644), (True, value, 0o755), "tc_1")]
    )
    assert len(result) == 1
    assert result[0]["change_kind"] == "modified"


def test_net_transition_kinds_and_true_noops():
    a, b = "a" * 64, "b" * 64
    created = coalesce_file_entries(
        [
            _state_entry("new.txt", (False, "", None), (True, a, 0o644), "tc_1"),
            _state_entry("new.txt", (True, a, 0o644), (True, b, 0o600), "tc_2"),
        ]
    )
    assert created[0]["change_kind"] == "created"

    deleted = coalesce_file_entries(
        [
            _state_entry("old.txt", (True, a, 0o644), (True, b, 0o600), "tc_1"),
            _state_entry("old.txt", (True, b, 0o600), (False, "", None), "tc_2"),
        ]
    )
    assert deleted[0]["change_kind"] == "deleted"

    assert coalesce_file_entries(
        [
            _state_entry("temp.txt", (False, "", None), (True, a, 0o644), "tc_1"),
            _state_entry("temp.txt", (True, a, 0o644), (False, "", None), "tc_2"),
        ]
    ) == []
    assert coalesce_file_entries(
        [
            _state_entry("same.txt", (True, a, 0o644), (True, b, 0o644), "tc_1"),
            _state_entry("same.txt", (True, b, 0o644), (True, a, 0o644), "tc_2"),
        ]
    ) == []


def test_ineligible_middle_entry_makes_whole_path_review_only():
    a, b, c, d = (char * 64 for char in "abcd")
    entries = [
        _state_entry("note.txt", (True, a, 0o644), (True, b, 0o644), "tc_1"),
        _state_entry("note.txt", (True, b, 0o644), (True, c, 0o644), "tc_2"),
        _state_entry("note.txt", (True, c, 0o644), (True, d, 0o644), "tc_3"),
    ]
    entries[1]["snapshot_eligible"] = False
    entries[1]["ineligible_reason"] = "binary_file"
    [merged] = coalesce_file_entries(entries)
    assert merged["snapshot_eligible"] is False
    assert merged["ineligible_reason"] == "discontinuous_history"
    assert merged["source_tool_change_ids"] == ["tc_1", "tc_2", "tc_3"]


def test_file_entry_rejects_inconsistent_hash_and_kind():
    value = "d" * 64
    entry = _state_entry(
        "note.txt", (True, value, 0o644), (True, value, 0o600), "tc_1"
    )
    entry["before_blob_ref"] = "f" * 64
    assert validate_file_entry(entry) == "blob_ref_hash_mismatch"

    entry = _state_entry(
        "note.txt", (True, value, 0o644), (True, value, 0o600), "tc_1"
    )
    entry["change_kind"] = "created"
    assert validate_file_entry(entry) == "change_kind_exists_mismatch"


def test_mode_unknown_review_entry_is_current_and_preserved():
    before, after = "a" * 64, "b" * 64
    entry = _state_entry(
        "note.txt", (True, before, 0o644), (True, after, 0o644), "tc_1"
    )
    entry.update(
        snapshot_eligible=False,
        ineligible_reason="mode_unknown",
        before_mode=None,
        after_mode=None,
        source_tool_change_ids=[],
    )
    assert validate_file_entry(entry) == ""
    [merged] = coalesce_file_entries([entry])
    assert merged["snapshot_eligible"] is False
    assert merged["ineligible_reason"] == "mode_unknown"
    assert merged["before_mode"] is None
    assert merged["after_mode"] is None


def test_new_ineligible_source_downgrades_net_history():
    before, after = "a" * 64, "b" * 64
    entry = _state_entry(
        "note.txt", (True, before, 0o644), (True, after, 0o644), "tc_1"
    )
    entry["snapshot_eligible"] = False
    entry["ineligible_reason"] = "sensitive_path"
    [merged] = coalesce_file_entries([entry])
    assert merged["snapshot_eligible"] is False
    assert merged["ineligible_reason"] == "discontinuous_history"


def test_new_source_ids_must_be_safe():
    value = "d" * 64
    entry = _state_entry(
        "note.txt", (True, value, 0o644), (True, value, 0o600), "../tc_1"
    )
    assert validate_file_entry(entry) == "invalid_sources"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda entry: entry.update(snapshot_eligible="yes"), "invalid_eligibility"),
        (lambda entry: entry.update(ineligible_reason=3), "invalid_eligibility"),
        (lambda entry: entry.update(ineligible_reason="blocked"), "invalid_eligibility"),
        (lambda entry: entry.update(before_mode=-1), "invalid_mode"),
        (lambda entry: entry.update(after_mode=0o10000), "invalid_mode"),
    ],
)
def test_file_entry_rejects_invalid_eligibility_and_modes(mutation, reason):
    value = "d" * 64
    entry = _state_entry(
        "note.txt", (True, value, 0o644), (True, value, 0o600), "tc_1"
    )
    mutation(entry)
    assert validate_file_entry(entry) == reason


def test_unusable_invalid_entry_is_not_emitted_as_an_invalid_review_entry():
    assert coalesce_file_entries([{}]) == []


def test_review_entry_never_copies_unsafe_source_ids():
    value = "d" * 64
    entry = _state_entry(
        "note.txt", (True, value, 0o644), (True, value, 0o600), "../forged"
    )
    [review] = coalesce_file_entries([entry])
    assert review["snapshot_eligible"] is False
    assert review["source_tool_change_ids"] == []


def test_turn_checkpoint_rejects_pending_source_history(tmp_path):
    store = CheckpointStore(tmp_path)
    pending = ToolChangeRecorder(store, owner_id="owner").start(
        "", "turn", "write_file", "workspace_write", {}
    )
    checkpoint = RecoveryCheckpointWriter(store, tmp_path).create_turn_checkpoint(
        session_id="session",
        run_id="run",
        turn_id="turn",
        parent_checkpoint_id="",
        tool_change_ids=[pending["tool_change_id"]],
    )
    assert checkpoint["integrity_errors"] == [
        {
            "reason": "incomplete_tool_change_history",
            "tool_change_ids": [pending["tool_change_id"]],
        }
    ]


def test_turn_checkpoint_records_missing_tool_changes_without_aborting(tmp_path):
    store = CheckpointStore(tmp_path)
    blob = store.write_blob(b"after\n", "text")
    tool_change = new_tool_change_record("tc_1", "", "task_1", "write_file", "workspace_write")
    tool_change["status"] = "finalized"
    tool_change["file_entries"] = [
        _state_entry(
            "note.txt",
            (False, "", None),
            (True, blob["content_hash"], 0o644),
            "tc_1",
        )
    ]
    store.write_tool_change_record(tool_change)

    writer = RecoveryCheckpointWriter(store, tmp_path)
    checkpoint = writer.create_turn_checkpoint(
        session_id="session_1",
        run_id="run_1",
        turn_id="task_1",
        parent_checkpoint_id="",
        tool_change_ids=["tc_1", "missing_tc"],
        verification_evidence=[],
    )

    loaded = store.load_checkpoint_record(checkpoint["checkpoint_id"])
    assert loaded["tool_change_ids"] == ["tc_1"]
    assert loaded["missing_tool_change_ids"] == ["missing_tc"]
    assert loaded["file_entries"][0]["path"] == "note.txt"
    assert loaded["integrity_errors"] == [
        {
            "reason": "incomplete_tool_change_history",
            "tool_change_ids": ["missing_tc"],
        }
    ]


def test_turn_checkpoint_rejects_forged_file_entry_provenance(tmp_path):
    store = CheckpointStore(tmp_path)
    value = store.write_blob(b"value")
    record = new_tool_change_record(
        "tc_1", "", "turn", "write_file", "workspace_write"
    )
    record["status"] = "finalized"
    record["file_entries"] = [
        _state_entry(
            "note.txt",
            (False, "", None),
            (True, value["content_hash"], 0o644),
            "tc_forged",
        )
    ]
    store.write_tool_change_record(record)
    checkpoint = RecoveryCheckpointWriter(store, tmp_path).create_turn_checkpoint(
        "session", "run", "turn", "", ["tc_1"]
    )
    assert checkpoint["tool_change_ids"] == []
    assert checkpoint["file_entries"] == []
    assert checkpoint["integrity_errors"] == [
        {
            "reason": "incomplete_tool_change_history",
            "tool_change_ids": ["tc_1"],
        }
    ]


def test_duplicate_requested_tool_change_is_stably_deduplicated(tmp_path):
    store = CheckpointStore(tmp_path)
    value = store.write_blob(b"value")
    record = new_tool_change_record(
        "tc_1", "", "turn", "write_file", "workspace_write"
    )
    record["status"] = "finalized"
    record["file_entries"] = [
        _state_entry(
            "note.txt",
            (False, "", None),
            (True, value["content_hash"], 0o644),
            "tc_1",
        )
    ]
    store.write_tool_change_record(record)
    checkpoint = RecoveryCheckpointWriter(store, tmp_path).create_turn_checkpoint(
        "session", "run", "turn", "", ["tc_1", "tc_1"]
    )
    assert checkpoint["tool_change_ids"] == ["tc_1"]
    assert len(checkpoint["file_entries"]) == 1


def test_turn_checkpoint_rejects_missing_source_field(tmp_path):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"before")
    after = store.write_blob(b"after")
    record = new_tool_change_record(
        "tc_legacy", "", "turn", "write_file", "workspace_write"
    )
    record["status"] = "finalized"
    entry = _state_entry(
        "note.txt",
        (True, before["content_hash"], 0o644),
        (True, after["content_hash"], 0o644),
        "tc_legacy",
    )
    entry.pop("source_tool_change_ids")
    record["file_entries"] = [entry]
    with pytest.raises(ValueError, match="invalid_file_entry"):
        store.write_tool_change_record(record)


def test_turn_checkpoint_accepts_explicit_empty_source_history(tmp_path):
    store = CheckpointStore(tmp_path)
    before = store.write_blob(b"before")
    after = store.write_blob(b"after")
    record = new_tool_change_record(
        "tc_current", "", "turn", "write_file", "workspace_write"
    )
    record["status"] = "finalized"
    entry = _state_entry(
        "note.txt",
        (True, before["content_hash"], 0o644),
        (True, after["content_hash"], 0o644),
        "tc_current",
    )
    entry["source_tool_change_ids"] = []
    record["file_entries"] = [entry]
    store.write_tool_change_record(record)
    checkpoint = RecoveryCheckpointWriter(store, tmp_path).create_turn_checkpoint(
        "session", "run", "turn", "", ["tc_current"]
    )
    assert checkpoint["tool_change_ids"] == ["tc_current"]
    assert checkpoint["integrity_errors"] == []
    assert checkpoint["file_entries"][0]["snapshot_eligible"] is True
