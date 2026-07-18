from pony import Pony
from pony.state.session_store import SessionStore
from benchmarks.support.fake_provider import FakeModelClient
from pony.context.renderer import render_current_user_message
from pony.agent.prompt_prefix import build_prompt_prefix, tool_signature
from pony.tools.registry import build_tool_registry
from pony.workspace.context import WorkspaceContext
from pony.runtime.options import RuntimeOptions


class _Agent:
    depth = 0
    max_depth = 1

    def __init__(self, root):
        self.root = root


def test_tool_signature_is_stable_across_registry_insertion_order(tmp_path):
    tools = {
        "b": {
            "schema": {"path": "str"},
            "risky": False,
            "description": "B",
            "run": object(),
        },
        "a": {
            "schema": {"command": "str"},
            "risky": True,
            "description": "A",
            "run": object(),
        },
    }
    reordered = {"a": tools["a"], "b": tools["b"]}

    assert tool_signature(tools) == tool_signature(reordered)


def test_tool_signature_binds_effect_class():
    base = {
        "read": {
            "schema": {},
            "risky": False,
            "effect_class": "read_only",
            "description": "Read.",
        }
    }
    changed = {
        "read": {
            **base["read"],
            "effect_class": "workspace_write",
        }
    }

    assert tool_signature(base) != tool_signature(changed)


def test_build_prompt_prefix_keeps_schemas_and_ordinary_docs_out_of_system(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Always run focused tests.\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    tools = build_tool_registry(_Agent(tmp_path))

    prefix = build_prompt_prefix(
        workspace=workspace, tools=tools, built_at="2026-06-02T00:00:00+08:00"
    )

    assert "You are pony" in prefix.text
    assert "Return at most one native tool call per response" in prefix.text
    assert "Available native tools:" not in prefix.text
    assert "read_file" not in prefix.text
    assert "Always run focused tests." in prefix.text
    assert "demo" not in prefix.text
    assert "path: str" not in prefix.text
    assert prefix.hash
    assert prefix.workspace_fingerprint == workspace.fingerprint()
    assert prefix.tool_signature == tool_signature(tools)
    assert prefix.built_at == "2026-06-02T00:00:00+08:00"


def test_stable_prefix_is_native_tool_protocol_neutral(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    tools = build_tool_registry(_Agent(tmp_path))

    prefix = build_prompt_prefix(workspace, tools).text

    assert "Return exactly one <tool>" not in prefix
    assert "<final>" not in prefix
    assert '<tool>{"name":' not in prefix


def test_memory_guidance_lives_once_in_prefix_not_current_user_request(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(approval_policy="auto"),
    )
    agent.session["messages"].append(
        {"role": "user", "content": "inspect the project", "_pony_meta": {}}
    )
    snapshot, telemetry = render_current_user_message(agent, "inspect the project")
    request, _ = agent.context_manager.build_request(
        injection_snapshot=snapshot,
        injection_telemetry=telemetry,
        preflight_metadata={},
    )
    current_user = request["messages"][-1]["content"]

    for opening_tag in ("<memory_usage_guidance>", "<memory_reading_guidance>"):
        assert agent.prefix.count(opening_tag) == 1
        assert opening_tag not in current_user
