from copy import deepcopy
import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch
from unittest.mock import Mock

import pytest

import pony as pony_pkg
import pony.cli.app as pony_cli
from pony.agent.loop import _commit_session, _plain_message
import pony.memory.service as memorylib
from pony.agent.messages import make_tool_pair, validate_messages
from pony.runtime.application import DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_MAX_STEPS
from pony.state.session_store import (
    LEGACY_SESSION_FORMAT_VERSION,
    SESSION_FORMAT_VERSION,
    SessionFormatError,
    SessionStore,
)
from pony import Pony
from pony.workspace.context import WorkspaceContext
from benchmarks.support.fake_provider import FakeModelClient
from pony.providers.response import Response, StopReason
from pony.runtime.options import RuntimeOptions
from pony.runtime.legacy import LegacySandboxResumeError


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    agent = Pony(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True, **kwargs),
    )
    agent._approval_prompt = lambda _name, _args: True
    return agent


def build_cli_agent(args):
    return pony_cli.build_agent(args, confirm=lambda _root: True)


def bound_fake_client(
    outputs,
    *,
    protocol_family="openai_responses",
    model="gpt-test",
    endpoint_hash_character="a",
):
    client = FakeModelClient(outputs)
    client.model = model
    client.provider_binding = {
        "protocol_family": protocol_family,
        "model": model,
        "endpoint_hash": "sha256:" + endpoint_hash_character * 64,
    }
    return client


def set_raw_file_summary(agent, path, summary):
    memorylib.set_file_summary_dict(
        agent.session["memory"]["file_summaries"],
        path,
        summary,
        workspace_root=agent.root,
    )


# =============================================================================
# Agent integration smoke tests
# =============================================================================


def test_pony_constructor_uses_coding_agent_defaults(tmp_path):
    agent = build_agent(tmp_path, [])

    assert agent.max_steps == DEFAULT_MAX_STEPS == 12
    assert agent.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS == 16_384


def test_new_runtime_persists_current_messages_only(tmp_path):
    agent = build_agent(tmp_path, ["done"])

    assert agent.ask("q") == "done"

    rows = [
        json.loads(line)
        for line in Path(agent.session_path).read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["record_type"] == "session_header"
    assert rows[0]["format_version"] == SESSION_FORMAT_VERSION
    assert all("history" not in row for row in rows)
    persisted = agent.session_store.load(agent.session["id"])
    validate_messages(persisted["messages"], require_meta=True)


def test_new_session_persists_provider_binding(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    client = bound_fake_client([])

    agent = Pony(
        model_client=client,
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True),
    )

    assert agent.session["provider_binding"] == client.provider_binding
    assert (
        store.load(agent.session["id"])["provider_binding"] == client.provider_binding
    )


def test_resume_rejects_a_different_model_session_binding(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    original = Pony(
        model_client=bound_fake_client([]),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True),
    )
    different = bound_fake_client(
        [],
        protocol_family="anthropic_messages",
        model="claude-test",
        endpoint_hash_character="b",
    )

    with pytest.raises(ValueError, match="model_session_mismatch"):
        Pony.from_session(
            model_client=different,
            workspace=workspace,
            session_store=store,
            session_id=original.session["id"],
            options=RuntimeOptions(project_trusted=True),
        )


def test_model_switch_persists_and_resumes_with_the_new_binding(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")

    def factory(model):
        return bound_fake_client([], model=model)

    agent = Pony(
        model_client=factory("gpt-test"),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(
            project_trusted=True,
            model_client_factory=factory,
        ),
    )

    changed = agent.set_model("gpt-next")

    assert changed["model"] == "gpt-next"
    assert agent.model_client.model == "gpt-next"
    assert store.load(agent.session["id"])["provider_binding"]["model"] == "gpt-next"
    resumed = Pony.from_session(
        model_client=factory("gpt-next"),
        workspace=workspace,
        session_store=store,
        session_id=agent.session["id"],
        options=RuntimeOptions(
            project_trusted=True,
            model_client_factory=factory,
        ),
    )
    assert resumed.current_model_binding()["model"] == "gpt-next"


def test_model_switch_rejects_session_change_during_client_factory(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    agent = None

    def factory(model):
        store.label(agent.session["id"], "concurrent factory change")
        return bound_fake_client([], model=model)

    agent = Pony(
        model_client=bound_fake_client([]),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(
            project_trusted=True,
            model_client_factory=factory,
        ),
    )

    with pytest.raises(SessionFormatError, match="^model_session_mismatch$"):
        agent.set_model("gpt-next")

    assert agent.model_client.model == "gpt-test"
    assert store.load(agent.session["id"])["provider_binding"]["model"] == "gpt-test"


@pytest.mark.parametrize(
    "provider_state",
    [
        [{
            "type": "thinking",
            "thinking": "summary",
            "signature": "opaque-signature",
        }],
        [{"type": "redacted_thinking", "data": "opaque-data"}],
    ],
)
def test_anthropic_provider_state_resumes_with_same_binding(
    tmp_path,
    provider_state,
):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    client = bound_fake_client(
        [],
        protocol_family="anthropic_messages",
        model="claude-test",
    )
    agent = Pony(
        model_client=client,
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True),
    )
    pair = make_tool_pair(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="anthropic-state",
        result_content="body",
        created_at="now",
        tool_status="ok",
        effect_class="read_only",
        provider_state=provider_state,
    )
    store.append_messages(agent.session["id"], pair)

    resumed = Pony.from_session(
        model_client=bound_fake_client(
            [],
            protocol_family="anthropic_messages",
            model="claude-test",
        ),
        workspace=workspace,
        session_store=store,
        session_id=agent.session["id"],
        options=RuntimeOptions(project_trusted=True),
    )

    assert resumed.current_model_binding() == client.provider_binding


@pytest.mark.parametrize(
    "candidate",
    [
        {
            "protocol_family": "openai_responses",
            "model": "claude-test",
            "endpoint_hash_character": "a",
        },
        {
            "protocol_family": "anthropic_messages",
            "model": "claude-next",
            "endpoint_hash_character": "a",
        },
        {
            "protocol_family": "anthropic_messages",
            "model": "claude-test",
            "endpoint_hash_character": "b",
        },
    ],
)
def test_anthropic_provider_state_rejects_different_binding(tmp_path, candidate):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    agent = Pony(
        model_client=bound_fake_client(
            [],
            protocol_family="anthropic_messages",
            model="claude-test",
        ),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True),
    )
    store.append_messages(
        agent.session["id"],
        make_tool_pair(
            name="read_file",
            arguments={"path": "README.md"},
            tool_use_id="anthropic-state",
            result_content="body",
            created_at="now",
            tool_status="ok",
            effect_class="read_only",
            provider_state=[{
                "type": "thinking",
                "thinking": "summary",
                "signature": "opaque-signature",
            }],
        ),
    )

    with pytest.raises(ValueError, match="^model_session_mismatch$"):
        Pony.from_session(
            model_client=bound_fake_client([], **candidate),
            workspace=workspace,
            session_store=store,
            session_id=agent.session["id"],
            options=RuntimeOptions(project_trusted=True),
        )


def test_turn_rejects_response_after_concurrent_model_switch(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")

    def factory(model):
        return bound_fake_client([], model=model)

    primary_client = factory("gpt-test")
    agents = {}

    def complete(**_kwargs):
        agents["concurrent"].set_model("gpt-next")
        return Response(
            stop_reason=StopReason.TOOL_USE,
            content=[{
                "type": "tool_use",
                "id": "stale-tool",
                "name": "read_file",
                "input": {"path": "README.md"},
            }],
            provider_state=[{
                "type": "reasoning",
                "encrypted_content": "opaque-state",
                "summary": [],
            }],
        )

    primary_client.complete = complete
    primary = Pony(
        model_client=primary_client,
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(
            project_trusted=True,
            model_client_factory=factory,
        ),
    )
    agents["concurrent"] = Pony.from_session(
        model_client=factory("gpt-test"),
        workspace=workspace,
        session_store=store,
        session_id=primary.session["id"],
        options=RuntimeOptions(
            project_trusted=True,
            model_client_factory=factory,
        ),
    )

    with pytest.raises(SessionFormatError, match="^model_session_mismatch$"):
        primary.ask("read the file")

    persisted = store.load(primary.session["id"])
    assert persisted["provider_binding"]["model"] == "gpt-next"
    assert all("_pony_provider_state" not in message for message in persisted["messages"])
    assert not any(
        message.get("_pony_meta", {}).get("tool_use_id") == "stale-tool"
        for message in persisted["messages"]
    )


@pytest.mark.parametrize("branch_operation", ["rewind", "fork"])
def test_model_switch_survives_branch_to_an_earlier_entry(
    tmp_path,
    branch_operation,
):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")

    def factory(model):
        return bound_fake_client([], model=model)

    agent = Pony(
        model_client=factory("gpt-test"),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(
            project_trusted=True,
            model_client_factory=factory,
        ),
    )
    earlier_entry = store.load_tree(agent.session["id"]).leaf_id
    agent.set_model("gpt-next")

    if branch_operation == "rewind":
        agent.rewind_session(earlier_entry)
    else:
        agent.fork_session(earlier_entry)

    assert agent.model_client.model == "gpt-next"
    assert agent.current_model_binding()["model"] == "gpt-next"
    assert store.load(agent.session["id"])["provider_binding"]["model"] == "gpt-next"


def test_model_switch_rejects_target_drift_without_writing(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    agent = Pony(
        model_client=bound_fake_client([]),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(
            project_trusted=True,
            model_client_factory=lambda model: bound_fake_client(
                [],
                model=model,
                endpoint_hash_character="b",
            ),
        ),
    )
    before = store.path(agent.session["id"]).read_bytes()

    with pytest.raises(ValueError, match="^model_session_mismatch$"):
        agent.set_model("gpt-next")

    assert store.path(agent.session["id"]).read_bytes() == before
    assert agent.current_model_binding()["model"] == "gpt-test"


def test_model_switch_rejects_opaque_provider_state_without_writing(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    agent = Pony(
        model_client=bound_fake_client([]),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(
            project_trusted=True,
            model_client_factory=lambda model: bound_fake_client([], model=model),
        ),
    )
    pair = make_tool_pair(
        name="read_file",
        arguments={"path": "README.md"},
        tool_use_id="state-call",
        result_content="body",
        created_at="now",
        tool_status="ok",
        effect_class="read_only",
        provider_state=[
            {
                "type": "reasoning",
                "encrypted_content": "opaque-state",
                "summary": [],
            }
        ],
    )
    store.append_messages(agent.session["id"], pair)
    agent._reload_session_projection()
    before = store.path(agent.session["id"]).read_bytes()

    with pytest.raises(ValueError, match="^model_session_mismatch$"):
        agent.set_model("gpt-next")

    assert store.path(agent.session["id"]).read_bytes() == before


def test_unbound_legacy_session_cannot_replay_provider_state(tmp_path):
    original = build_agent(tmp_path, [])
    original.session["messages"] = list(
        make_tool_pair(
            name="read_file",
            arguments={"path": "README.md"},
            tool_use_id="legacy-state-call",
            result_content="body",
            created_at="now",
            tool_status="ok",
            effect_class="read_only",
            provider_state=[
                {
                "type": "reasoning",
                "encrypted_content": "opaque-state",
                "summary": [],
                }
            ],
        )
    )
    original.session_store.save(original.session)

    with pytest.raises(ValueError, match="model_session_mismatch"):
        Pony.from_session(
            model_client=bound_fake_client(["done"]),
            workspace=original.workspace,
            session_store=original.session_store,
            session_id=original.session["id"],
            options=RuntimeOptions(project_trusted=True),
        )


def test_unbound_session_cannot_resume_with_a_bound_model(tmp_path):
    original = build_agent(tmp_path, [])
    client = bound_fake_client(["done"])
    with pytest.raises(ValueError, match="model_session_mismatch"):
        Pony.from_session(
            model_client=client,
            workspace=original.workspace,
            session_store=original.session_store,
            session_id=original.session["id"],
            options=RuntimeOptions(project_trusted=True),
        )


def test_commit_session_keeps_memory_and_disk_on_same_safe_payload(tmp_path):
    secret = "sk-session-secret-123456789"
    agent = build_agent(tmp_path, [])
    agent.memory.set_task_summary(secret)
    agent._sync_working_memory()

    _commit_session(agent, messages=(_plain_message("user", secret),))

    persisted = agent.session_store.load(agent.session["id"])
    assert secret not in json.dumps(agent.session)
    assert agent.session["messages"] == persisted["messages"]
    assert persisted["working_memory"] == {
        "task_summary": "",
        "recent_files": [],
    }
    assert secret not in json.dumps(agent.memory.to_dict())


def test_turn_start_sanitizes_before_memory_and_task_state(tmp_path):
    secret = "github_pat_A123456789012345678901234567890"
    agent = build_agent(tmp_path, ["safe"])

    agent.ask(secret)

    assert secret not in json.dumps(agent.memory.to_dict())
    assert secret not in json.dumps(agent.current_task_state.to_dict())


def test_programmatic_resume_sanitizes_process_secret_before_first_request(
    tmp_path,
    monkeypatch,
):
    secret = "opaque-process-value-123456789"
    monkeypatch.setenv("PONY_TEST_API_KEY", secret)
    original = build_agent(tmp_path, [])
    raw = dict(original.session)
    raw["messages"] = [
        {"role": "user", "content": secret, "_pony_meta": {"created_at": "test"}}
    ]
    raw["format_version"] = 1
    raw.pop("permission_mode")
    original.session_store.path(raw["id"]).unlink()
    legacy = original.session_store.legacy_path(raw["id"])
    legacy.write_text(json.dumps(raw), encoding="utf-8")
    legacy.chmod(0o600)
    client = FakeModelClient(["<final>safe</final>"])
    resume_store = SessionStore(original.session_store.root)

    resumed = Pony.from_session(
        model_client=client,
        workspace=original.workspace,
        session_store=resume_store,
        session_id=raw["id"],
        options=RuntimeOptions(project_trusted=True),
    )
    resumed.ask("continue")

    assert secret not in json.dumps(client.requests)
    assert secret not in json.dumps(resumed.session)
    assert isinstance(resumed.redaction_env, MappingProxyType)
    with pytest.raises(TypeError):
        resumed.redaction_env["MUTATE"] = "blocked"


def test_programmatic_resume_requires_transient_bypass_capability(tmp_path):
    agent = build_agent(
        tmp_path,
        [],
        allow_dangerously_skip_permissions=True,
    )
    agent.set_permission_mode("bypassPermissions")
    resume_store = SessionStore(agent.session_store.root)

    with pytest.raises(ValueError, match="requires dangerous capability"):
        Pony.from_session(
            model_client=FakeModelClient([]),
            workspace=agent.workspace,
            session_store=resume_store,
            session_id=agent.session["id"],
            options=RuntimeOptions(project_trusted=True),
        )

    resumed = Pony.from_session(
        model_client=FakeModelClient([]),
        workspace=agent.workspace,
        session_store=resume_store,
        session_id=agent.session["id"],
        options=RuntimeOptions(
            project_trusted=True,
            allow_dangerously_skip_permissions=True,
        ),
    )
    assert resumed.current_permission_mode() == "bypassPermissions"


def test_programmatic_resume_rejects_legacy_sandbox_binding_before_session_load(
    tmp_path, monkeypatch
):
    agent = build_agent(tmp_path, [])
    monkeypatch.setattr(
        "pony.runtime.application.preflight_legacy_sandbox_resume",
        lambda *_args: (_ for _ in ()).throw(
            LegacySandboxResumeError("legacy_sandbox_session_unsupported")
        ),
    )
    monkeypatch.setattr(
        agent.session_store,
        "load_for_resume",
        lambda *_args: pytest.fail("session load must not run"),
    )

    with pytest.raises(LegacySandboxResumeError) as caught:
        Pony.from_session(
            model_client=FakeModelClient([]),
            workspace=agent.workspace,
            session_store=agent.session_store,
            session_id=agent.session["id"],
            options=RuntimeOptions(project_trusted=True),
        )

    assert caught.value.code == "legacy_sandbox_session_unsupported"


def test_programmatic_resume_rejects_invalid_legacy_binding_before_session_load(
    tmp_path, monkeypatch
):
    agent = build_agent(tmp_path, [])
    monkeypatch.setattr(
        "pony.runtime.application.preflight_legacy_sandbox_resume",
        lambda *_args: (_ for _ in ()).throw(
            LegacySandboxResumeError(
                "sandbox_state_invalid", reason_code="sandbox_manifest_invalid"
            )
        ),
    )
    monkeypatch.setattr(
        agent.session_store,
        "load_for_resume",
        lambda *_args: pytest.fail("session load must not run"),
    )

    with pytest.raises(LegacySandboxResumeError) as caught:
        Pony.from_session(
            model_client=FakeModelClient([]),
            workspace=agent.workspace,
            session_store=agent.session_store,
            session_id=agent.session["id"],
            options=RuntimeOptions(project_trusted=True),
        )

    assert caught.value.code == "sandbox_state_invalid"
    assert caught.value.reason_code == "sandbox_manifest_invalid"


def test_programmatic_resume_validates_session_id_before_legacy_preflight(
    tmp_path, monkeypatch
):
    agent = build_agent(tmp_path, [])
    monkeypatch.setattr(
        "pony.runtime.application.preflight_legacy_sandbox_resume",
        lambda *_args: pytest.fail("legacy preflight must not run"),
    )

    with pytest.raises(ValueError, match="invalid session id"):
        Pony.from_session(
            model_client=FakeModelClient([]),
            workspace=agent.workspace,
            session_store=agent.session_store,
            session_id="../invalid",
            options=RuntimeOptions(project_trusted=True),
        )


def test_direct_session_constructor_rejects_legacy_binding_before_side_effects(
    tmp_path, monkeypatch
):
    agent = build_agent(tmp_path, [])
    monkeypatch.setattr(
        "pony.runtime.application.preflight_legacy_sandbox_resume",
        lambda *_args: (_ for _ in ()).throw(
            LegacySandboxResumeError("legacy_sandbox_session_unsupported")
        ),
    )
    monkeypatch.setattr(
        Pony,
        "_configure_workspace",
        lambda *_args: pytest.fail("workspace configuration must not run"),
    )
    monkeypatch.setattr(
        agent.session_store,
        "save",
        lambda *_args: pytest.fail("session writer must not run"),
    )
    monkeypatch.setattr(
        agent.session_store,
        "append_messages",
        lambda *_args: pytest.fail("session writer must not run"),
    )

    with pytest.raises(LegacySandboxResumeError) as caught:
        Pony(
            model_client=FakeModelClient([]),
            workspace=agent.workspace,
            session_store=agent.session_store,
            session=agent.session,
            options=RuntimeOptions(project_trusted=True),
        )

    assert caught.value.code == "legacy_sandbox_session_unsupported"


def test_direct_session_constructor_validates_session_id_before_legacy_preflight(
    tmp_path, monkeypatch
):
    agent = build_agent(tmp_path, [])
    invalid_session = deepcopy(agent.session)
    invalid_session["id"] = "../invalid"
    monkeypatch.setattr(
        "pony.runtime.application.preflight_legacy_sandbox_resume",
        lambda *_args: pytest.fail("legacy preflight must not run"),
    )

    with pytest.raises(ValueError, match="invalid session id"):
        Pony(
            model_client=FakeModelClient([]),
            workspace=agent.workspace,
            session_store=agent.session_store,
            session=invalid_session,
            options=RuntimeOptions(project_trusted=True),
        )


def test_direct_session_constructor_rejects_bypass_without_capability(tmp_path):
    agent = build_agent(
        tmp_path,
        [],
        allow_dangerously_skip_permissions=True,
        delegate_model_client_factory=lambda: FakeModelClient([]),
    )
    agent.set_permission_mode("bypassPermissions")

    with pytest.raises(ValueError, match="requires dangerous capability"):
        Pony(
            model_client=FakeModelClient([]),
            workspace=agent.workspace,
            session_store=agent.session_store,
            session=agent.session,
            options=RuntimeOptions(project_trusted=True),
        )


def test_programmatic_resume_rejects_plan_that_would_restore_bypass(tmp_path):
    agent = build_agent(
        tmp_path,
        [],
        allow_dangerously_skip_permissions=True,
    )
    agent.set_permission_mode("bypassPermissions")
    agent.set_permission_mode("plan")

    with pytest.raises(ValueError, match="requires dangerous capability"):
        Pony.from_session(
            model_client=FakeModelClient([]),
            workspace=agent.workspace,
            session_store=agent.session_store,
            session_id=agent.session["id"],
            options=RuntimeOptions(project_trusted=True),
        )


def test_bypass_capability_is_read_only_and_not_inherited_by_delegate(
    tmp_path, monkeypatch
):
    agent = build_agent(
        tmp_path,
        [],
        allow_dangerously_skip_permissions=True,
        delegate_model_client_factory=lambda: FakeModelClient([]),
    )
    children = []

    with pytest.raises(AttributeError):
        agent.bypass_permissions_available = False

    monkeypatch.setattr(
        Pony,
        "ask",
        lambda child, _task: children.append(child) or "done",
    )
    agent.spawn_delegate({"task": "inspect", "max_steps": 1})

    assert children[0].bypass_permissions_available is False


@pytest.mark.parametrize(
    ("original_mode", "capability", "expected_pre_mode"),
    (
        ("auto", False, "auto"),
        ("bypassPermissions", True, "bypassPermissions"),
        ("bypassPermissions", False, "default"),
    ),
)
def test_programmatic_resume_into_plan_preserves_only_authorized_pre_mode(
    tmp_path,
    original_mode,
    capability,
    expected_pre_mode,
):
    agent = build_agent(
        tmp_path,
        [],
        allow_dangerously_skip_permissions=(
            original_mode == "bypassPermissions"
        ),
    )
    agent.set_permission_mode(original_mode)

    resumed = Pony.from_session(
        model_client=FakeModelClient([]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        resume_permission_mode="plan",
        options=RuntimeOptions(
            project_trusted=True,
            allow_dangerously_skip_permissions=capability,
        ),
    )

    assert resumed.current_permission_mode() == "plan"
    assert resumed.session["pre_plan_mode"] == expected_pre_mode


def test_supplied_redaction_proxy_is_copied_before_backing_mutation(tmp_path):
    secret = "opaque-proxy-value-123456789"
    backing = {"PONY_TEST_API_KEY": secret}
    supplied = MappingProxyType(backing)
    agent = build_agent(
        tmp_path,
        [],
        redaction_env=supplied,
    )

    backing["PONY_TEST_API_KEY"] = "replacement-value-123456789"

    assert agent.redaction_env["PONY_TEST_API_KEY"] == secret
    assert agent.redaction_env is not supplied
    assert agent.redact_text(secret) == "<redacted>"


def test_delegate_reuses_snapshot_without_replacing_shared_store_redactors(
    tmp_path,
    monkeypatch,
):
    secret = "opaque-delegate-value-123456789"
    agent = build_agent(
        tmp_path,
        [],
        redaction_env=MappingProxyType({"PONY_TEST_API_KEY": secret}),
        delegate_model_client_factory=lambda: FakeModelClient([]),
    )
    session_redactor = agent.session_store._redactor
    run_redactor = agent.run_store._redactor
    assert getattr(session_redactor, "__self__", None) is None
    assert getattr(run_redactor, "__self__", None) is None
    children = []

    def fake_ask(child, task):
        children.append(child)
        return "safe"

    monkeypatch.setattr(Pony, "ask", fake_ask)

    assert agent.spawn_delegate({"task": "inspect", "max_steps": 1}) == (
        "delegate_result:\nsafe"
    )

    assert children[0].redaction_env is agent.redaction_env
    assert agent.session_store._redactor is session_redactor
    assert agent.run_store._redactor is run_redactor
    safe = session_redactor({"payload": secret})
    assert secret not in json.dumps(safe)


def test_supplied_legacy_session_is_rejected_outside_store_migration(
    tmp_path,
):
    secret = "github_pat_A123456789012345678901234567890"
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    raw_session = {
        "record_type": "session",
        "format_version": LEGACY_SESSION_FORMAT_VERSION,
        "id": "direct-raw",
        "created_at": "2026-01-01T00:00:00+00:00",
        "workspace_root": str(tmp_path),
        "messages": [{"role": "user", "content": secret, "_pony_meta": {}}],
        "working_memory": {"task_summary": secret, "recent_files": []},
        "memory": {},
        "recently_recalled": [],
        "checkpoints": {},
        "resume_state": {},
        "runtime_identity": {},
    }

    with pytest.raises(ValueError, match="current session"):
        Pony(
            model_client=FakeModelClient([]),
            workspace=workspace,
            session_store=store,
            session=raw_session,
            options=RuntimeOptions(project_trusted=True),
        )


def test_runtime_rejects_dead_prompt_cache_feature_flag(tmp_path):
    with pytest.raises(ValueError, match="unsupported feature flag"):
        build_agent(tmp_path, [], feature_flags={"prompt_cache": True})


def test_repeated_tool_detection_reads_canonical_tool_use_blocks(tmp_path):
    agent = build_agent(tmp_path, [])
    pairs = []
    for index, path in enumerate(("a.py", "b.py", "a.py", "b.py")):
        pairs.extend(
            make_tool_pair(
            name="read_file",
            arguments={"path": path},
            tool_use_id=f"tu_{index}",
            result_content="body",
            created_at="t",
            tool_status="ok",
            effect_class="read_only",
            )
        )
    agent.session["messages"].extend(pairs)

    assert agent.repeated_tool_call("read_file", {"path": "a.py"}) is True
    assert agent.repeated_tool_call("read_file", {"path": "c.py"}) is False


def test_reset_clears_transient_state_and_preserves_permission_and_audit(tmp_path):
    agent = build_agent(tmp_path, ["done"])
    agent.ask("q")
    session_id = agent.session["id"]
    agent.set_permission_mode("plan")
    durable_checkpoint_items = deepcopy(agent.session["checkpoints"]["items"])
    agent.session["recently_recalled"] = ["note"]
    agent.session["_recall_errors"] = {"count": 2, "last": "x"}
    agent.session["working_memory"] = {
        "task_summary": "goal",
        "recent_files": ["a.py"],
    }
    agent.session["memory"] = {"file_summaries": {"a.py": {"summary": "fact"}}}
    agent.session["resume_state"] = {"status": "full-valid"}
    agent.reset()

    assert agent.session["id"] == session_id
    assert agent.session["messages"] == []
    assert agent.session["recently_recalled"] == []
    assert "_recall_errors" not in agent.session
    assert agent.session["working_memory"] == {"task_summary": "", "recent_files": []}
    assert agent.session["memory"] == {"file_summaries": {}}
    assert agent.session["checkpoints"]["current_id"] == ""
    assert agent.session["checkpoints"]["items"] == durable_checkpoint_items
    assert agent.session["resume_state"] == {}
    assert "recovery" not in agent.session
    assert agent.session["permission_mode"] == "plan"


def test_agent_runs_tool_then_final(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {"name": "read_file", "args": {"path":"hello.txt","start":1,"end":2}},
            "Read the file successfully.",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Read the file successfully."
    assert any(
        message["role"] == "assistant"
        and isinstance(message["content"], list)
        and message["content"][0].get("type") == "tool_use"
        and message["content"][0].get("name") == "read_file"
        for message in agent.session["messages"]
    )
    assert "hello.txt" in agent.session["working_memory"]["recent_files"]
    assert "hello.txt" in agent.session["memory"]["file_summaries"]


def test_agent_updates_task_summary_on_each_request(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "First pass.",
            "Second pass.",
        ],
    )

    assert agent.ask("First request") == "First pass."
    assert agent.session["working_memory"]["task_summary"] == "First request"

    assert agent.ask("Second request") == "Second pass."
    assert agent.session["working_memory"]["task_summary"] == "Second request"


def test_agent_stores_file_summaries_without_episodic_notes(tmp_path):
    (tmp_path / "facts.txt").write_text("deploy key is red\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            {"name": "read_file", "args": {"path":"facts.txt","start":1,"end":1}},
            "Done.",
            "It is red.",
        ],
    )

    assert agent.ask("Read the file and remember the fact") == "Done."
    assert "facts.txt" in agent.session["working_memory"]["recent_files"]
    assert "deploy key is red" in agent.session["memory"]["file_summaries"]["facts.txt"]
    checkpoint = agent.current_checkpoint()
    assert any(
        item.get("path") == "facts.txt"
        and "deploy key is red" in item.get("summary", "")
        for item in checkpoint["key_files"]
    )
    assert "episodic_notes" not in agent.session["memory"]
    assert "notes" not in agent.session["memory"]

    resumed = Pony.from_session(
        model_client=FakeModelClient(["It is red."]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        options=RuntimeOptions(project_trusted=True),
    )

    assert resumed.ask("What color is the deploy key?") == "It is red."
    assert "episodic_notes" not in resumed.session["memory"]
    assert "notes" not in resumed.session["memory"]


def test_file_summary_cache_is_invalidated_on_out_of_band_edit_and_path_spelling(
    tmp_path,
):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    set_raw_file_summary(agent, "./sample.txt", "sample.txt: alpha")
    agent.memory.remember_file("./sample.txt")
    agent._sync_working_memory()
    agent.session_store.save(agent.session)
    assert agent.session["memory"]["file_summaries"]["sample.txt"]["freshness"]

    file_path.write_text("beta\n", encoding="utf-8")

    resumed = Pony.from_session(
        model_client=FakeModelClient([]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        options=RuntimeOptions(project_trusted=True),
    )

    assert "sample.txt" not in resumed.session["memory"]["file_summaries"]


def test_agent_retries_after_empty_model_output(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "Recovered after retry.",
        ],
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after retry."
    notice = "model returned no actionable content"
    assert not any(notice in str(item["content"]) for item in agent.session["messages"])
    feedback_requests = [
        index
        for index, request in enumerate(agent.model_client.requests)
        if "<pony:runtime_feedback>" in json.dumps(request)
    ]
    assert feedback_requests == [1]
    assert notice in json.dumps(agent.model_client.requests[1])


def test_agent_retries_after_malformed_tool_payload(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                        "type": "tool_use",
                        "id": "bad_call",
                        "name": "read_file",
                        "input": "bad",
                    }
                ],
            ),
            {"name": "read_file", "args": {"path":"hello.txt","start":1,"end":1}},
            "Recovered after malformed tool output.",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Recovered after malformed tool output."
    assert any(
        message["role"] == "assistant"
        and isinstance(message["content"], list)
        and message["content"][0].get("type") == "tool_use"
        and message["content"][0].get("name") == "read_file"
        for message in agent.session["messages"]
    )
    notice = "native tool call had an invalid name or arguments object"
    assert not any(notice in str(item["content"]) for item in agent.session["messages"])
    feedback_requests = [
        index
        for index, request in enumerate(agent.model_client.requests)
        if "<pony:runtime_feedback>" in json.dumps(request)
    ]
    assert feedback_requests == [1]
    assert notice in json.dumps(agent.model_client.requests[1])


def test_agent_never_executes_text_tool_markup(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.py"><content>print("hi")\n</content></tool>',
            "Done.",
        ],
    )

    answer = agent.ask("Create hello.py")

    assert answer.startswith('<tool name="write_file"')
    assert not (tmp_path / "hello.py").exists()


def test_one_protocol_correction_can_recover(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "Recovered after one correction.",
        ],
        max_steps=1,
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after one correction."


def test_agent_saves_and_resumes_session(tmp_path):
    agent = build_agent(tmp_path, ["First pass."])
    assert agent.ask("Start a session") == "First pass."

    resumed = Pony.from_session(
        model_client=FakeModelClient(["Resumed."]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        options=RuntimeOptions(project_trusted=True),
    )

    assert resumed.session["messages"][0]["content"] == "Start a session"
    assert resumed.ask("Continue") == "Resumed."


def test_delegate_uses_child_agent(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            {"name": "delegate", "args": {"task":"inspect README","max_steps":2}},
            "Parent incorporated the child result.",
        ],
        delegate_model_client_factory=lambda: FakeModelClient(["Child result."]),
    )

    answer = agent.ask("Use delegation")

    assert answer == "Parent incorporated the child result."
    tool_results = [
        message["content"][0]
        for message in agent.session["messages"]
        if message["role"] == "user"
        and isinstance(message["content"], list)
        and message["content"][0].get("type") == "tool_result"
    ]
    assert "delegate_result" in tool_results[0]["content"]


def test_patch_file_replaces_exact_match(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello world\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "patch_file",
        {
            "path": "sample.txt",
            "old_text": "world",
            "new_text": "agent",
        },
    )

    assert result == "patched sample.txt"
    assert file_path.read_text(encoding="utf-8") == "hello agent\n"


def test_invalid_risky_tool_does_not_prompt_for_approval(tmp_path):
    agent = build_agent(tmp_path, [])

    with patch("builtins.input") as mock_input:
        result = agent.run_tool("write_file", {})

    assert result.startswith("error: invalid arguments for write_file: 'path'")
    assert 'example: {"name":"write_file","arguments":' in result
    mock_input.assert_not_called()


def test_list_files_hides_internal_agent_state(tmp_path):
    agent = build_agent(tmp_path, [])
    (tmp_path / ".pony").mkdir(exist_ok=True)
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")

    result = agent.run_tool("list_files", {})

    assert ".pony" not in result
    assert ".git" not in result
    assert "[F] hello.txt" in result


def test_repeated_identical_tool_call_is_rejected(tmp_path):
    agent = build_agent(tmp_path, [])
    for index in range(2):
        agent.session["messages"].extend(
            make_tool_pair(
            name="list_files",
            arguments={},
            tool_use_id=f"tu_{index}",
            result_content="(empty)",
            created_at=str(index),
            tool_status="ok",
            effect_class="read_only",
            )
        )

    result = agent.run_tool("list_files", {})

    assert (
        result
        == "error: repeated identical tool call for list_files; choose a different tool or return a final answer"
    )


def test_repeated_tool_call_rejects_short_alternating_loops(tmp_path):
    agent = build_agent(tmp_path, [])
    calls = [
        ("list_files", {}, "(empty)"),
        ("read_file", {"path": "README.md", "start": 1, "end": 1}, "demo"),
        ("list_files", {}, "(empty)"),
        ("read_file", {"path": "README.md", "start": 1, "end": 1}, "demo"),
    ]
    for index, (name, arguments, content) in enumerate(calls):
        agent.session["messages"].extend(
            make_tool_pair(
            name=name,
            arguments=arguments,
            tool_use_id=f"tu_{index}",
            result_content=content,
            created_at=str(index),
            tool_status="ok",
            effect_class="read_only",
            )
        )

    result = agent.run_tool("list_files", {})

    assert (
        result
        == "error: repeated identical tool call for list_files; choose a different tool or return a final answer"
    )


# =============================================================================
# Build agent / fixed model configuration tests
# =============================================================================


def test_build_arg_parser_has_no_provider_backend_selection_flags(tmp_path):
    parser = pony_cli.build_arg_parser()
    destinations = {action.dest for action in parser._actions}

    assert {
        "provider",
        "profile",
        "auth_mode",
        "base_url",
        "host",
        "connection",
        "api",
        "api_key_env",
    }.isdisjoint(destinations)


def test_build_agent_uses_resolved_openai_client_and_project_env(tmp_path):
    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=openai-chat\n"
        "PONY_MODEL=claude-sonnet-4-6\n"
        "PONY_API_BASE=https://gateway.example/v1\n"
        "PONY_API_KEY=sk-project\n",
        encoding="utf-8",
    )
    args = pony_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True):
        with patch("pony.cli.assembly.build_transport_client") as model_client:
            fake_client = model_client.return_value
            agent = build_cli_agent(args)

    model_client.assert_called_once()
    assert model_client.call_args.args == ("openai_chat_completions",)
    assert model_client.call_args.kwargs == {
        "model": "claude-sonnet-4-6",
        "base_url": "https://gateway.example/v1",
        "api_key": "sk-project",
        "timeout": 300,
        "auth_mode": "bearer",
        "capabilities": {},
    }
    assert agent.model_client is fake_client


def test_build_agent_uses_process_env_when_project_env_is_missing(tmp_path):
    args = pony_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(
        os.environ,
        {
            "HOME": str(tmp_path),
            "PONY_PROVIDER": "openai-chat",
            "PONY_MODEL": "claude-sonnet-4-6",
            "PONY_API_BASE": "https://process.example/v1",
            "PONY_API_KEY": "sk-process",
        },
        clear=True,
    ):
        with patch("pony.cli.assembly.build_transport_client") as model_client:
            build_cli_agent(args)

    assert model_client.call_args.kwargs["base_url"] == "https://process.example/v1"
    assert model_client.call_args.kwargs["api_key"] == "sk-process"


def test_build_agent_switches_provider_from_generic_environment(tmp_path):
    args = pony_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(
        os.environ,
        {
            "HOME": str(tmp_path),
            "PONY_PROVIDER": "openai",
            "PONY_MODEL": "gpt-test",
            "PONY_API_BASE": "https://api.openai.com/v1",
            "PONY_API_KEY": "sk-openai",
        },
        clear=True,
    ):
        with patch("pony.cli.assembly.build_transport_client") as model_client:
            build_cli_agent(args)

    assert model_client.call_args.args == ("openai_responses",)
    assert model_client.call_args.kwargs["model"] == "gpt-test"
    assert model_client.call_args.kwargs["api_key"] == "sk-openai"
    assert model_client.call_args.kwargs["auth_mode"] == "bearer"


def test_build_agent_model_flag_overrides_model_without_writing_env(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PONY_PROVIDER=openai-responses\n"
        "PONY_API_BASE=https://api.openai.com/v1\n"
        "PONY_API_KEY=test-key\n"
        "PONY_MODEL=configured-model\n",
        encoding="utf-8",
    )
    before = env_path.read_bytes()
    args = pony_cli.build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--model", "gpt-next"]
    )

    with (
        patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True),
        patch("pony.cli.assembly.build_transport_client", side_effect=_fake_transport),
    ):
        agent = build_cli_agent(args)

    assert agent.current_model_binding()["model"] == "gpt-next"
    assert env_path.read_bytes() == before


def _fake_transport(protocol, **kwargs):
    client = FakeModelClient([])
    endpoint_hash = hashlib.sha256(kwargs["base_url"].encode("utf-8")).hexdigest()
    client.model = kwargs["model"]
    client.provider_binding = {
        "protocol_family": protocol,
        "model": kwargs["model"],
        "endpoint_hash": f"sha256:{endpoint_hash}",
    }
    return client


def test_build_agent_detects_missing_provider_without_writing_project_env(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PONY_API_BASE=https://gateway.example/v1\n"
        "PONY_API_KEY=test-key\n"
        "PONY_MODEL=gateway-model\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    before = env_path.stat()
    before_bytes = env_path.read_bytes()
    reports = iter(
        [
            {
                "status": "failed",
                "stage": "tool_call",
                "category": "response_invalid",
                "model_calls": 1,
                "usage_status": "degraded",
                "error_code": "provider_protocol_mismatch",
            },
            {
                "status": "ok",
                "stage": "complete",
                "category": "ok",
                "model_calls": 2,
                "usage_status": "complete",
            },
        ]
    )
    builder = Mock(side_effect=_fake_transport)
    args = pony_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with (
        patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True),
        patch("pony.cli.assembly.build_transport_client", builder),
        patch(
            "pony.providers.probe.probe_model_client",
            side_effect=lambda _client: next(reports),
        ),
    ):
        agent = build_cli_agent(args)

    after = env_path.stat()
    assert agent.model_client.provider_binding["protocol_family"] == "openai_responses"
    assert [call.args[0] for call in builder.call_args_list] == [
        "openai_chat_completions",
        "openai_responses",
        "openai_responses",
    ]
    assert env_path.read_bytes() == before_bytes
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_resume_reuses_current_provider_binding_without_probe(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    base_url = "https://gateway.example/v1"
    model = "gateway-model"
    original = Pony(
        model_client=_fake_transport(
            "openai_chat_completions",
            model=model,
            base_url=base_url,
        ),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True),
    )
    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=auto\n"
        f"PONY_API_BASE={base_url}\n"
        "PONY_API_KEY=test-key\n"
        "PONY_MODEL=configured-model\n",
        encoding="utf-8",
    )
    builder = Mock(side_effect=_fake_transport)
    args = pony_cli.build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--resume", original.session["id"]]
    )

    with (
        patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True),
        patch("pony.cli.assembly.build_transport_client", builder),
        patch(
            "pony.providers.probe.probe_model_client",
            side_effect=AssertionError("matching Session binding was probed"),
        ),
    ):
        resumed = build_cli_agent(args)

    assert resumed.session["id"] == original.session["id"]
    assert builder.call_args.args == ("openai_chat_completions",)
    assert builder.call_args.kwargs["model"] == model


def test_resume_model_flag_switches_binding_without_changing_env(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    base_url = "https://gateway.example/v1"
    original = Pony(
        model_client=_fake_transport(
            "openai_chat_completions",
            model="gateway-model",
            base_url=base_url,
        ),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True),
    )
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PONY_PROVIDER=auto\n"
        f"PONY_API_BASE={base_url}\n"
        "PONY_API_KEY=test-key\n"
        "PONY_MODEL=gateway-model\n",
        encoding="utf-8",
    )
    before = env_path.read_bytes()
    builder = Mock(side_effect=_fake_transport)
    args = pony_cli.build_arg_parser().parse_args(
        [
            "--cwd",
            str(tmp_path),
            "--resume",
            original.session["id"],
            "--model",
            "gateway-next",
        ]
    )

    with (
        patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True),
        patch("pony.cli.assembly.build_transport_client", builder),
    ):
        resumed = build_cli_agent(args)

    assert resumed.current_model_binding()["model"] == "gateway-next"
    assert resumed.model_client.model == "gateway-next"
    assert store.load(original.session["id"])["provider_binding"]["model"] == (
        "gateway-next"
    )
    assert env_path.read_bytes() == before
    assert [call.kwargs["model"] for call in builder.call_args_list] == [
        "gateway-model",
        "gateway-next",
    ]


def test_resume_auto_rejects_non_loopback_ollama_binding(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    base_url = "https://local-model.example/v1"
    model = "local-model"
    original = Pony(
        model_client=_fake_transport(
            "ollama_chat",
            model=model,
            base_url=base_url,
        ),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True),
    )
    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=auto\n"
        f"PONY_API_BASE={base_url}\n"
        "PONY_API_KEY=\n"
        f"PONY_MODEL={model}\n",
        encoding="utf-8",
    )
    args = pony_cli.build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--resume", original.session["id"]]
    )

    with (
        patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True),
        patch(
            "pony.providers.probe.probe_model_client",
            side_effect=AssertionError("mismatched binding was probed"),
        ),
        pytest.raises(ValueError, match="^model_session_mismatch$"),
    ):
        build_cli_agent(args)


def test_resume_auth_binding_without_key_fails_before_client_build(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    base_url = "https://gateway.example/v1"
    model = "gateway-model"
    original = Pony(
        model_client=_fake_transport(
            "openai_chat_completions",
            model=model,
            base_url=base_url,
        ),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True),
    )
    (tmp_path / ".env").write_text(
        "PONY_PROVIDER=auto\n"
        f"PONY_API_BASE={base_url}\n"
        "PONY_API_KEY=\n"
        f"PONY_MODEL={model}\n",
        encoding="utf-8",
    )
    args = pony_cli.build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--resume", original.session["id"]]
    )

    with (
        patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True),
        patch(
            "pony.cli.assembly.build_transport_client",
            side_effect=AssertionError("missing key built a client"),
        ),
        pytest.raises(ValueError, match="api_key_not_configured"),
    ):
        build_cli_agent(args)


@pytest.mark.parametrize(
    "override",
    (
        {"PONY_API_BASE": "https://other.example/v1"},
        {"PONY_PROVIDER": "openai-responses"},
    ),
)
def test_resume_binding_mismatch_fails_before_network(tmp_path, override):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    base_url = "https://gateway.example/v1"
    model = "gateway-model"
    original = Pony(
        model_client=_fake_transport(
            "openai_chat_completions",
            model=model,
            base_url=base_url,
        ),
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(project_trusted=True),
    )
    values = {
        "PONY_PROVIDER": "auto",
        "PONY_API_BASE": base_url,
        "PONY_API_KEY": "test-key",
        "PONY_MODEL": model,
        **override,
    }
    (tmp_path / ".env").write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()),
        encoding="utf-8",
    )
    args = pony_cli.build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--resume", original.session["id"]]
    )

    with (
        patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True),
        patch(
            "pony.cli.assembly.build_transport_client",
            side_effect=AssertionError("mismatch built a client"),
        ),
        patch(
            "pony.providers.probe.probe_model_client",
            side_effect=AssertionError("mismatch was probed"),
        ),
        pytest.raises(ValueError, match="model_session_mismatch"),
    ):
        build_cli_agent(args)


# =============================================================================
# Runtime/report/resume tests
# =============================================================================
# Runtime/report/resume tests moved to tests/test_runtime_report.py.


# =============================================================================
# Build agent / arg parser / packaging tests
# =============================================================================


def test_public_api_exports_resolve_through_package_path():
    assert Pony is not None
    assert SessionStore is not None
    assert WorkspaceContext is not None
    assert Path(pony_pkg.__file__).as_posix().endswith("/pony/__init__.py")


def test_package_import_surface_excludes_cli_entrypoints():
    assert not hasattr(pony_pkg, "main")
    assert not hasattr(pony_pkg, "build_agent")
    assert not hasattr(pony_pkg, "build_arg_parser")


def test_pony_does_not_initialize_legacy_recovery_components(tmp_path):
    agent = build_agent(tmp_path, outputs=["ok"])

    assert not hasattr(agent, "checkpoint_store")
    assert not hasattr(agent, "tool_change_recorder")
    assert not hasattr(agent, "recovery_checkpoint_writer")
    assert not hasattr(agent, "recovery_manager")


def test_module_execution_help_works():
    result = subprocess.run(
        [sys.executable, "-m", "pony", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
