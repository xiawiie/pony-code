from copy import deepcopy
import json
import os

import pytest

from pony.agent.messages import make_tool_pair
from pony.state.session_store import (
    PREVIOUS_SESSION_FORMAT_VERSION,
    SESSION_FORMAT_VERSION,
    SessionFormatError,
    SessionMigrationRequired,
    SessionStore,
    UnsupportedLegacyEntry,
)
from pony.state.workflow import (
    EMPTY_PLAN,
    PlanValidationError,
    SensitivePlanError,
    parse_plan_json,
    plan_digest,
)


def _session(workspace, session_id="workflow"):
    return {
        "record_type": "session",
        "format_version": SESSION_FORMAT_VERSION,
        "id": session_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "workspace_root": str(workspace),
        "messages": [],
        "working_memory": {"task_summary": "", "recent_files": []},
        "memory": {"file_summaries": {}},
        "recently_recalled": [],
        "checkpoints": {"current_id": "", "items": {}},
        "resume_state": {},
        "recovery": {"current_checkpoint_id": ""},
        "runtime_identity": {},
        "workflow_mode": "act",
        "active_plan": deepcopy(EMPTY_PLAN),
    }


def _plan():
    return {
        "goal": "Ship workflow state",
        "items": [
            {"id": "validate", "text": "Validate state", "status": "completed"},
            {"id": "persist", "text": "Persist state", "status": "in_progress"},
        ],
    }


def _rewrite_as_v2(path, *, model_change=False):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row["format_version"] = PREVIOUS_SESSION_FORMAT_VERSION
        if row.get("type") == "session_info":
            row["data"]["set"]["format_version"] = PREVIOUS_SESSION_FORMAT_VERSION
    if model_change:
        rows.append(
            {
                "record_type": "session_entry",
                "format_version": PREVIOUS_SESSION_FORMAT_VERSION,
                "id": "a" * 24,
                "parent_id": rows[-1]["id"],
                "timestamp": "2026-01-01T00:00:01+00:00",
                "type": "model_change",
                "data": {},
            }
        )
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    return rows


def test_plan_json_is_strict_canonical_and_bounded():
    plan = _plan()
    parsed = parse_plan_json(json.dumps(plan))

    assert parsed == plan
    assert plan_digest(parsed).startswith("sha256:")
    with pytest.raises(PlanValidationError, match="duplicate JSON key"):
        parse_plan_json('{"goal":"x","goal":"y","items":[]}')
    with pytest.raises(PlanValidationError, match="multiple in_progress"):
        parse_plan_json(
            json.dumps(
                {
                    "goal": "x",
                    "items": [
                        {"id": "1", "text": "a", "status": "in_progress"},
                        {"id": "2", "text": "b", "status": "in_progress"},
                    ],
                }
            )
        )
    with pytest.raises(PlanValidationError, match="12 KiB"):
        parse_plan_json(" " * (12 * 1024 + 1))


@pytest.mark.parametrize(
    "plan",
    [
        {"goal": "x", "items": [], "extra": True},
        {
            "goal": "x",
            "items": [
                {"id": "1", "text": "a", "status": "pending", "extra": True}
            ],
        },
        {
            "goal": "x",
            "items": [
                {"id": "same", "text": "a", "status": "pending"},
                {"id": "same", "text": "b", "status": "completed"},
            ],
        },
        {
            "goal": "x",
            "items": [{"id": "1", "text": "a", "status": "blocked"}],
        },
        {
            "goal": "x",
            "items": [
                {"id": str(index), "text": "a", "status": "pending"}
                for index in range(13)
            ],
        },
        {
            "goal": "x" * 301,
            "items": [{"id": "1", "text": "a", "status": "pending"}],
        },
        {
            "goal": "x",
            "items": [{"id": "1", "text": "x" * 301, "status": "pending"}],
        },
        {
            "goal": "bad\x00goal",
            "items": [{"id": "1", "text": "a", "status": "pending"}],
        },
        {
            "goal": "x",
            "items": [{"id": "1", "text": "bad\x7ftext", "status": "pending"}],
        },
        {"goal": "", "items": [{"id": "1", "text": "a", "status": "pending"}]},
        {"goal": "not empty", "items": []},
    ],
)
def test_plan_json_rejects_schema_and_boundary_violations(plan):
    with pytest.raises(PlanValidationError):
        parse_plan_json(json.dumps(plan))


def test_plan_json_rejects_redactor_changes_as_sensitive_content():
    with pytest.raises(SensitivePlanError) as raised:
        parse_plan_json(
            json.dumps(_plan()),
            redactor=lambda value: {**value, "goal": "<redacted>"},
        )

    assert raised.value.code == "sensitive_content_block"


def test_active_path_projects_controls_and_successful_atomic_plan_tool(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    store.save(_session(tmp_path))
    base_leaf = store.load_tree("workflow").leaf_id
    store.set_workflow_mode("workflow", "review")
    store.set_active_plan("workflow", _plan())
    tool_plan = {
        "goal": "Review result",
        "items": [{"id": "review", "text": "Review diff", "status": "pending"}],
    }
    pair = make_tool_pair(
        name="update_plan",
        arguments={"plan_json": json.dumps(tool_plan)},
        tool_use_id="plan-1",
        result_content="updated",
        created_at="2026-01-01T00:00:01+00:00",
        tool_status="ok",
        effect_class="session_state",
    )
    store.append_messages("workflow", pair)

    projection = store.load("workflow")
    assert projection["workflow_mode"] == "review"
    assert projection["active_plan"] == tool_plan

    store.fork("workflow", base_leaf)
    projection = store.load("workflow")
    assert projection["workflow_mode"] == "act"
    assert projection["active_plan"] == EMPTY_PLAN


def test_rejected_or_invalid_plan_tool_never_changes_durable_plan(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = store.save(_session(tmp_path))
    rejected = make_tool_pair(
        name="update_plan",
        arguments={"plan_json": "not json"},
        tool_use_id="plan-rejected",
        result_content="rejected",
        created_at="2026-01-01T00:00:01+00:00",
        tool_status="rejected",
        effect_class="session_state",
    )
    store.append_messages("workflow", rejected)
    assert store.load("workflow")["active_plan"] == EMPTY_PLAN

    before = path.read_bytes()
    invalid = make_tool_pair(
        name="update_plan",
        arguments={"plan_json": "not json"},
        tool_use_id="plan-invalid",
        result_content="updated",
        created_at="2026-01-01T00:00:02+00:00",
        tool_status="ok",
        effect_class="session_state",
    )
    with pytest.raises(SessionFormatError, match="invalid_plan"):
        store.append_messages("workflow", invalid)
    assert path.read_bytes() == before


def test_v2_inspection_is_read_only_and_resume_preserves_tree_structure(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = store.save(_session(tmp_path, "old-tree"))
    old_rows = _rewrite_as_v2(path)
    before = path.stat()

    storage, projection, tree = store.inspect_readonly("old-tree")

    after = path.stat()
    assert storage == "legacy_jsonl"
    assert projection["format_version"] == PREVIOUS_SESSION_FORMAT_VERSION
    assert tree.leaf_id == old_rows[-1]["id"]
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    with pytest.raises(SessionMigrationRequired):
        store.label("old-tree", "blocked")

    migrated = store.load_for_resume("old-tree")
    new_rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert migrated["workflow_mode"] == "act"
    assert migrated["active_plan"] == EMPTY_PLAN
    assert [{**row, "format_version": 0} for row in old_rows] == [
        {**row, "format_version": 0} for row in new_rows
    ]
    assert list((store.root / "legacy-backups").glob("old-tree.*.jsonl"))


def test_v1_inspection_is_read_only_and_creates_no_migration_artifacts(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    legacy = _session(tmp_path, "legacy-readonly")
    legacy["format_version"] = 1
    legacy.pop("workflow_mode")
    legacy.pop("active_plan")
    path = store.legacy_path("legacy-readonly")
    path.write_text(json.dumps(legacy), encoding="utf-8")
    path.chmod(0o600)
    store.lock_path.touch(mode=0o600)
    before = path.stat()

    storage, projection, tree = store.inspect_readonly("legacy-readonly")

    after = path.stat()
    assert storage == "legacy"
    assert projection == legacy
    assert tree is None
    assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_mode,
    )
    assert not store.candidate_path("legacy-readonly").exists()
    assert not (store.root / "legacy-backups").exists()


def test_v2_model_change_fails_before_writing_migration_artifacts(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = store.save(_session(tmp_path, "unsupported"))
    original = _rewrite_as_v2(path, model_change=True)

    with pytest.raises(UnsupportedLegacyEntry, match="model_change"):
        store.load_for_resume("unsupported")

    assert [json.loads(line) for line in path.read_text().splitlines()] == original
    assert not store.candidate_path("unsupported").exists()
    assert not (store.root / "legacy-backups").exists()


def test_v2_publish_failure_keeps_source_and_resume_is_retryable(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    path = store.save(_session(tmp_path, "retry-v2"))
    original = _rewrite_as_v2(path)
    replace = os.replace

    def fail_candidate_publish(source, destination, **kwargs):
        if str(source).endswith(".jsonl.candidate"):
            raise OSError("candidate publish failed")
        return replace(source, destination, **kwargs)

    monkeypatch.setattr("pony.state.session_store.os.replace", fail_candidate_publish)
    with pytest.raises(OSError, match="candidate publish failed"):
        store.load_for_resume("retry-v2")
    assert [json.loads(line) for line in path.read_text().splitlines()] == original

    monkeypatch.setattr("pony.state.session_store.os.replace", replace)
    assert store.load_for_resume("retry-v2")["format_version"] == SESSION_FORMAT_VERSION
