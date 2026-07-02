"""Stable prompt prefix construction."""

import hashlib
import json
import textwrap
from dataclasses import dataclass

from .tools import TOOL_EXAMPLES
from .workspace import now


@dataclass
class PromptPrefix:
    # prefix 除了文本本身，还带一小份元数据，
    # 这样 runtime 才能明确判断 prefix 是否可以复用。
    text: str
    hash: str
    workspace_fingerprint: str
    tool_signature: str
    built_at: str


TOOL_EXAMPLE_ORDER = ("list_files", "read_file", "search", "write_file", "patch_file", "run_shell", "delegate")


def tool_signature(tools):
    payload = []
    for name in sorted(tools):
        tool = tools[name]
        payload.append(
            {
                "name": name,
                "schema": tool["schema"],
                "risky": tool["risky"],
                "description": tool["description"],
            }
        )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _tool_specific_rules(tools):
    available = set(tools)
    lines = []
    if "write_file" in available:
        lines.extend(
            [
                "- For write_file with multi-line text, prefer XML style:",
                '  <tool name="write_file" path="file.py"><content>...</content></tool>',
            ]
        )
    if "patch_file" in available:
        lines.extend(
            [
                "- For patch_file with multi-line text, prefer XML style:",
                '  <tool name="patch_file" path="file.py"><old_text>old</old_text><new_text>new</new_text></tool>',
            ]
        )
    file_edit_tools = [name for name in ("write_file", "patch_file") if name in available]
    if file_edit_tools:
        lines.append(
            f"- If the user asks you to create or update a specific file and the path is clear, use {' or '.join(file_edit_tools)} instead of repeatedly listing files."
        )
    required_arg_tools = [name for name in ("read_file", "search", "write_file", "patch_file", "run_shell", "delegate") if name in available]
    if required_arg_tools:
        lines.append(
            f"- Required tool arguments must not be empty. Do not call {', '.join(required_arg_tools)} with args={{}}."
        )
    return "\n".join(lines)


def _response_examples(tools):
    examples = [
        TOOL_EXAMPLES[name]
        for name in TOOL_EXAMPLE_ORDER
        if name in tools and name in TOOL_EXAMPLES
    ]
    examples.append("<final>Done.</final>")
    return "\n".join(examples)


def build_prompt_prefix(workspace, tools, built_at=None):
    tool_lines = []
    for name, tool in tools.items():
        fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
        risk = "approval required" if tool["risky"] else "safe"
        tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
    tool_text = "\n".join(tool_lines)
    tool_specific_rules = _tool_specific_rules(tools)
    examples = _response_examples(tools)
    # prefix 可以理解成 agent 的“工作手册”：
    # 它是谁、工具怎么调用、当前仓库的稳定事实，都写在这里。
    # workspace 的易变部分（branch/status/commits）走 volatile section，不进 stable prefix。
    text = textwrap.dedent(
        f"""\
        You are pico, a small local coding agent working inside a local repository.

        Rules:
        - Use tools instead of guessing about the workspace.
        - Return exactly one <tool>...</tool> or one <final>...</final>.
        - Tool calls must look like:
          <tool>{{"name":"tool_name","args":{{...}}}}</tool>
        - Final answers must look like:
          <final>your answer</final>
        - Never invent tool results.
        - Keep answers concise and concrete.
        - Before writing tests for existing code, read the implementation first.
        - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
        - New files should be complete and runnable, including obvious imports.
        - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
        {tool_specific_rules}

        Tools:
        {tool_text}

        Valid response examples:
        {examples}

        {workspace.stable_text()}
        """
    ).strip()
    signature = tool_signature(tools)
    return PromptPrefix(
        text=text,
        hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        workspace_fingerprint=workspace.fingerprint(),
        tool_signature=signature,
        built_at=built_at or now(),
    )
