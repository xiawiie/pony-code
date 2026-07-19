from types import SimpleNamespace

from pony.agent.compaction import compact_session, rewind_with_branch_summary
from pony.context.renderer import InjectionSnapshot
from pony.agent.context_manager import ContextManager
from pony.agent.messages import make_tool_pair
from pony.agent.model_capabilities import (
    ModelCapabilities,
    TokenAccounting,
    build_model_budget,
)
from benchmarks.support.fake_provider import FakeModelClient
from pony.state.session_store import SessionStore, entry_message_refs
from pony.state.session_store import SESSION_FORMAT_VERSION
from pony.workspace.context import now


def _session(workspace):
    return {
        "record_type": "session",
        "format_version": SESSION_FORMAT_VERSION,
        "id": "long-session",
        "created_at": now(),
        "workspace_root": str(workspace),
        "messages": [],
        "working_memory": {},
        "memory": {},
        "recently_recalled": [],
        "checkpoints": {},
        "resume_state": {},
        "recovery": {},
        "runtime_identity": {},
        "permission_mode": "auto",
        "permission_rules": {"allow": [], "ask": [], "deny": []},
        "plan_text": "",
        "plan_revision": 0,
        "pre_plan_mode": "",
    }


def _plain(role, text):
    return {
        "role": role,
        "content": text,
        "_pony_meta": {"created_at": now()},
    }


def _agent(workspace, store):
    capabilities = ModelCapabilities(
        context_window=128_000,
        max_output_tokens=16_384,
        token_counter_mode="provider_usage_or_estimate",
        source="config",
    )
    return SimpleNamespace(
        session={"id": "long-session", "messages": []},
        session_store=store,
        token_accounting=TokenAccounting(),
        model_capabilities=capabilities,
        model_budget=build_model_budget(
            capabilities,
            keep_recent_tokens=1_000,
        ),
        model_client=FakeModelClient(
            [
                "# Goal\nContinue 200-turn task\n# Next Steps\nProcess later turns",
                "# Goal\nFinish long task\n# Next Steps\nChoose the active branch",
                "# Abandoned Approach\nDiscarded branch\n"
                "# Discoveries & Decisions\nKeep the stable base\n"
                "# File Operations\nNo file changes\n"
                "# Facts to Carry Forward\nThe second compaction remains active",
            ]
        ),
        redact_text=lambda value: str(value),
        redaction_env={},
        secret_env_names=(),
        prefix="system",
        tools={},
        visible_tools=lambda: {},
        context_config={"compaction": {"enabled": True}},
        _pending_token_anchor=None,
    )


def test_200_turn_session_compacts_twice_branches_and_resumes(tmp_path):
    store = SessionStore(tmp_path / ".pony" / "sessions")
    store.save(_session(tmp_path))
    agent = _agent(tmp_path, store)

    for turn in range(200):
        batch = [_plain("user", f"turn-{turn} goal " + ("detail " * 12))]
        if turn < 50:
            batch.extend(
                make_tool_pair(
                    name="read_file",
                    arguments={"path": f"src/file-{turn}.py"},
                    tool_use_id=f"tool-{turn}",
                    result_content=f"result-{turn} " + ("body " * 12),
                    created_at=now(),
                    tool_status="ok",
                    effect_class="workspace_read",
                )
            )
        batch.append(_plain("assistant", f"turn-{turn} answer " + ("decision " * 10)))
        store.append_messages("long-session", batch)
        if turn == 119:
            first = compact_session(agent, reason="long_session_first")
            assert first.tokens_after < first.tokens_before

    second = compact_session(agent, reason="long_session_second")
    assert second.tokens_after < second.tokens_before

    store.append_messages(
        "long-session",
        (_plain("user", "branch base"), _plain("assistant", "stable base")),
    )
    branch_target = store.load_tree("long-session").leaf_id
    store.append_messages(
        "long-session",
        (
            _plain("user", "abandoned experiment"),
            _plain("assistant", "abandoned result"),
        ),
    )
    abandoned_leaf = store.load_tree("long-session").leaf_id
    branch = rewind_with_branch_summary(
        agent,
        branch_target,
        focus="carry stable facts",
    )
    store.append_messages(
        "long-session",
        (_plain("user", "new branch"), _plain("assistant", "continue safely")),
    )

    tree = store.load_tree("long-session")
    checkpoint = {
        "checkpoint_id": "ckpt-long",
        "created_at": now(),
        "goal": "finish the long session",
        "status": "in_progress",
        "completed": ["200 scripted turns"],
        "in_progress": ["active branch"],
        "blocker": "",
        "next_steps": ["resume safely"],
        "key_files": [],
        "read_files": [],
        "modified_files": [],
        "workspace_checkpoint_id": "",
        "worktree_identity_digest": tree.header["worktree_identity"]["digest"],
        "context_usage": {},
    }
    store.append_task_checkpoint("long-session", checkpoint)
    tree = store.load_tree("long-session")
    agent.session = store.load("long-session")

    raw_messages = sum(len(entry_message_refs(entry)) for entry in tree.entries)
    assert raw_messages == 506
    assert sum(entry["type"] == "tool_exchange" for entry in tree.entries) == 50
    assert sum(entry["type"] == "compaction" for entry in tree.entries) == 2
    assert abandoned_leaf in {entry["id"] for entry in tree.entries}
    assert abandoned_leaf not in {entry["id"] for entry in tree.active_path}
    assert branch.rewind_entry in tree.active_path
    assert branch.summary_entry in tree.active_path
    assert tree.projection["checkpoints"]["current_id"] == "ckpt-long"

    snapshot = InjectionSnapshot(
        current_user="continue",
        runtime_feedback="",
        allocator_name="priority_allocator",
        sources=(),
    )
    request, metadata = ContextManager(agent).build_request(
        injection_snapshot=snapshot,
        injection_telemetry={},
        preflight_metadata={},
    )
    breakdown = metadata["context_breakdown"]
    assert breakdown["budget"]["used"] <= breakdown["budget"]["input_limit"]
    assert breakdown["history"]["dropped_turns"] == 0
    assert "<pony:session_summary>" in request["messages"][0]["content"]
    assert any(
        "<pony:branch_summary>" in str(message.get("content", ""))
        for message in request["messages"]
    )

    path = store.path("long-session")
    assert path.read_bytes().endswith(b"\n")
    assert len(path.read_bytes().splitlines()) == len(tree.entries) + 1
    resumed_store = SessionStore(tmp_path / ".pony" / "sessions")
    resumed = resumed_store.load_tree("long-session")
    assert resumed.leaf_id == tree.leaf_id
    assert [entry["id"] for entry in resumed.active_path] == [
        entry["id"] for entry in tree.active_path
    ]
    assert resumed_store.context_view("long-session").summary == (
        store.context_view("long-session").summary
    )
    assert resumed.projection["checkpoints"]["current_id"] == "ckpt-long"
    assert resumed.header["worktree_identity"] == tree.header["worktree_identity"]
