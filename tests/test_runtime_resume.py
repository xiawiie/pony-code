from pony.runtime.resume import (
    active_prompt_history,
    build_permission_request_metadata,
    build_resume_projection,
)


def _session():
    return {
        "permission_mode": "plan",
        "checkpoints": {
            "current_id": "internal-checkpoint-id",
            "items": {
                "internal-checkpoint-id": {
                    "goal": "Checkpoint goal",
                    "status": "in_progress",
                    "blocker": "waiting in /private/repo for review",
                    "next_steps": ["run /private/repo focused tests"],
                    "key_files": [{"path": "/private/repo/secret.py"}],
                }
            },
        },
        "resume_state": {
            "status": "partial-stale",
            "stale_paths": ["/private/repo/secret.py"],
            "runtime_identity_mismatch_fields": ["cwd"],
        },
        "provider_binding": {
            "protocol_family": "openai_responses",
            "model": "gpt-test",
            "endpoint_hash": "sha256:" + "a" * 64,
        },
    }


def test_permission_request_metadata_contains_only_bounded_permission_facts():
    metadata = build_permission_request_metadata(_session(), visible_tool_count=7)

    assert metadata == {
        "permission_mode": "plan",
        "visible_tool_count": 7,
    }


def test_resume_projection_labels_sources_and_omits_internal_identifiers_and_paths():
    projection = build_resume_projection(_session())

    assert projection["goal"] == {
        "text": "Checkpoint goal",
        "source": "checkpoint",
    }
    assert projection["permission_mode"] == "plan"
    assert projection["checkpoint"]["source"] == "checkpoint"
    assert projection["checkpoint"]["blocker"] == "waiting in <path> for review"
    assert projection["checkpoint"]["next_steps"] == ["run <path> focused tests"]
    assert projection["resume"]["stale_path_count"] == 1
    assert projection["model"]["model"] == "gpt-test"
    rendered = repr(projection)
    assert "internal-checkpoint-id" not in rendered
    assert "/private/repo" not in rendered
    assert "endpoint_hash" not in rendered


def test_resume_projection_falls_back_to_checkpoint_goal_and_applies_redactor():
    session = _session()
    session["checkpoints"]["items"]["internal-checkpoint-id"]["goal"] = (
        "token=concrete-value"
    )

    projection = build_resume_projection(
        session,
        redactor=lambda value: {
            **value,
            "goal": {"text": "<redacted>", "source": value["goal"]["source"]},
        },
    )

    assert projection["goal"] == {"text": "<redacted>", "source": "checkpoint"}


def test_active_prompt_history_keeps_only_recent_complete_plain_user_entries():
    messages = [
        {"role": "user", "content": f"old-{index}"}
        for index in range(110)
    ]
    messages.extend(
        [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool", "content": "result"}
                ],
            },
            {"role": "assistant", "content": "assistant text"},
            {"role": "user", "content": "x" * (16 * 1024 + 1)},
            {"role": "user", "content": "latest"},
        ]
    )

    history = active_prompt_history(messages)

    assert len(history) == 100
    assert history[0] == "old-11"
    assert history[-1] == "latest"
    assert "result" not in history
    assert "assistant text" not in history


def test_active_prompt_history_enforces_utf8_total_bytes_and_accepts_rebuilt_branch():
    abandoned = {"role": "user", "content": "abandoned branch"}
    active_messages = [
        {"role": "user", "content": "a" * (16 * 1024)} for _ in range(5)
    ]
    active_messages.append({"role": "user", "content": "active leaf"})

    history = active_prompt_history(active_messages)

    assert abandoned["content"] not in history
    assert history == ["a" * (16 * 1024)] * 3 + ["active leaf"]
    assert sum(len(item.encode("utf-8")) for item in history) <= 64 * 1024
