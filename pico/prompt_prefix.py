"""Stable prompt prefix construction."""

import hashlib
import json
import textwrap
from dataclasses import dataclass

from .workspace import now


MEMORY_USAGE_GUIDANCE = """<memory_usage_guidance>
- Use memory_save ONLY when the user explicitly asks to remember/save something.
- Do NOT save routine tool results, file paths, or turn-scoped state.
- Good candidates: cross-session lessons, design decisions, environment gotchas.
- Bad candidates: "I read auth.py", "current turn diff", "pytest passed".
</memory_usage_guidance>"""

MEMORY_READING_GUIDANCE = """<memory_reading_guidance>
Before answering about the codebase or a past decision:
- If <memory_index> shows a relevant file, consider memory_read.
- For symbol location, prefer repo_lookup over manual search.
- For keyword lookup across notes, use memory_search.
</memory_reading_guidance>"""


@dataclass
class PromptPrefix:
    # prefix 除了文本本身，还带一小份元数据，
    # 这样 runtime 才能明确判断 prefix 是否可以复用。
    text: str
    hash: str
    workspace_fingerprint: str
    tool_signature: str
    built_at: str


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


def build_prompt_prefix(workspace, tools, built_at=None):
    tool_lines = []
    for name, tool in tools.items():
        fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
        risk = "approval required" if tool["risky"] else "safe"
        tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
    tool_text = "\n".join(tool_lines)
    tool_specific_rules = _tool_specific_rules(tools)
    # prefix 可以理解成 agent 的“工作手册”：
    # 它是谁、工具怎么调用、当前仓库的稳定事实，都写在这里。
    # workspace 的易变部分（branch/status/commits）走 volatile section，不进 stable prefix。
    text = textwrap.dedent(
        f"""\
        You are pico, a small local coding agent working inside a local repository.

        Rules:
        - Use the provided native tools instead of guessing about the workspace.
        - Never invent tool results.
        - Keep answers concise and concrete.
        - Before writing tests for existing code, read the implementation first.
        - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
        - New files should be complete and runnable, including obvious imports.
        - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
        {tool_specific_rules}

        Tools:
        {tool_text}

        {MEMORY_USAGE_GUIDANCE}

        {MEMORY_READING_GUIDANCE}

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
