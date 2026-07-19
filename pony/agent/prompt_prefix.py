"""Stable prompt prefix construction."""

import hashlib
import json
import textwrap
from dataclasses import dataclass

from pony.workspace.context import now


MEMORY_USAGE_GUIDANCE = """<memory_usage_guidance>
- Save durable memory ONLY when the user explicitly asks to remember/save something.
- Do NOT save routine tool results, file paths, or turn-scoped state.
- Good candidates: cross-session lessons, design decisions, environment gotchas.
- Bad candidates: "I read auth.py", "current turn diff", "pytest passed".
</memory_usage_guidance>"""

MEMORY_READING_GUIDANCE = """<memory_reading_guidance>
Before answering about the codebase or a past decision:
- If <memory_index> shows a relevant file, consider reading it.
- Prefer indexed symbol lookup over manual inspection when available.
- Prefer keyword lookup across notes when available.
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
                "effect_class": tool.get("effect_class", "workspace_write"),
                "description": tool["description"],
            }
        )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def build_prompt_prefix(workspace, tools, built_at=None):
    # Provider tool schemas are the only capability list. The pinned prefix
    # keeps stable behavior and applicable AGENTS files.
    text = textwrap.dedent(
        f"""\
        You are pony, a small local coding agent working inside a local repository.

        Rules:
        - Use the provided native tools instead of guessing about the workspace.
        - Return at most one native tool call per response. Only delegate_worktrees may batch independent tasks.
        - Never invent tool results.
        - Keep answers concise and concrete.
        - Before writing tests for existing code, read the implementation first.
        - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
        - New files should be complete and runnable, including obvious imports.
        - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
        {MEMORY_USAGE_GUIDANCE}

        {MEMORY_READING_GUIDANCE}

        {workspace.instruction_text()}
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
