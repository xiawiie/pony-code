from pico.prompt_prefix import build_prompt_prefix, tool_signature
from pico.tools import build_tool_registry
from pico.workspace import WorkspaceContext


class _Agent:
    depth = 0
    max_depth = 1

    def __init__(self, root):
        self.root = root


def test_tool_signature_is_stable_across_registry_insertion_order(tmp_path):
    tools = {
        "b": {"schema": {"path": "str"}, "risky": False, "description": "B", "run": object()},
        "a": {"schema": {"command": "str"}, "risky": True, "description": "A", "run": object()},
    }
    reordered = {"a": tools["a"], "b": tools["b"]}

    assert tool_signature(tools) == tool_signature(reordered)


def test_build_prompt_prefix_renders_tools_and_workspace_metadata(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    tools = build_tool_registry(_Agent(tmp_path))

    prefix = build_prompt_prefix(workspace=workspace, tools=tools, built_at="2026-06-02T00:00:00+08:00")

    assert "You are pico" in prefix.text
    assert "Tools:" in prefix.text
    assert "- read_file(" in prefix.text
    assert "Workspace:" in prefix.text
    assert prefix.hash
    assert prefix.workspace_fingerprint == workspace.fingerprint()
    assert prefix.tool_signature == tool_signature(tools)
    assert prefix.built_at == "2026-06-02T00:00:00+08:00"
