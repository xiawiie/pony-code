"""The explicit, auditable registry of model-visible tools."""

from functools import partial
import re
import unicodedata

from pony.memory.repo_map import tool_repo_lookup
from pony.memory.tools import (
    tool_memory_list,
    tool_memory_read,
    tool_memory_save,
    tool_memory_search,
)
from .files import tool_list_files, tool_patch_file, tool_read_file, tool_write_file
from .search import tool_search
from .shell import DEFAULT_RUN_SHELL_TIMEOUT, _tool_run_shell


_ALLOWED_EFFECT_CLASSES = frozenset(
    {"read_only", "workspace_write", "memory_write", "session_state"}
)


def memory_write_intent(current_user, *, history=(), delegated=False):
    """保守识别当前用户输入中的显式持久记忆意图。"""
    del history  # 历史请求不得向当前 turn 继承授权。
    if delegated:
        return False
    text = unicodedata.normalize("NFKC", str(current_user or "")).strip().casefold()
    if not text or re.search(
        r"\b(?:do not|don't|dont|never)\s+(?:please\s+)?remember\b", text
    ):
        return False
    if re.match(r"^/remember(?:\s|:|$)", text):
        return True
    if re.match(r"^(?:请记住|请保存到记忆|请存入记忆)(?:\s|[:：]|$|[一-鿿])", text):
        return True
    if re.match(r"^(?:remember|please remember)(?:\s|:|$)", text):
        return True
    return bool(
        re.match(r"^(?:please\s+)?save\b.+\b(?:to|in)\s+(?:the\s+)?memory\b", text)
    )


BASE_TOOL_SPECS = {
    "list_files": {
        "schema": {"path": "str='.'"},
        "risky": False,
        "effect_class": "read_only",
        "description": "List files in the workspace.",
    },
    "read_file": {
        "schema": {"path": "str", "start": "int=1", "end": "int=200"},
        "risky": False,
        "effect_class": "read_only",
        "description": "Read a UTF-8 file by line range.",
    },
    "search": {
        "schema": {"pattern": "str", "path": "str='.'"},
        "risky": False,
        "effect_class": "read_only",
        "description": "Search the workspace with rg or a simple fallback.",
    },
    "run_shell": {
        "schema": {"command": "str", "timeout": f"int={DEFAULT_RUN_SHELL_TIMEOUT}"},
        "risky": True,
        "effect_class": "workspace_write",
        "description": "Run a shell command in the repo root.",
    },
    "write_file": {
        "schema": {"path": "str", "content": "str"},
        "risky": True,
        "effect_class": "workspace_write",
        "description": "Write a text file.",
    },
    "patch_file": {
        "schema": {"path": "str", "old_text": "str", "new_text": "str"},
        "risky": True,
        "effect_class": "workspace_write",
        "description": "Replace one exact text block in a file.",
    },
    "memory_list": {
        "schema": {"prefix": "str=''"},
        "risky": False,
        "effect_class": "read_only",
        "description": "List memory files (user notes + agent_notes). Optional prefix filter.",
    },
    "memory_read": {
        "schema": {"path": "str", "start": "int=1", "end": "int=200"},
        "risky": False,
        "effect_class": "read_only",
        "description": "Read a memory file by line range. Same paging as read_file.",
    },
    "memory_search": {
        "schema": {"query": "str", "limit": "int=5"},
        "risky": False,
        "effect_class": "read_only",
        "description": "Full-text search across memory files (BM25 + CJK bigram). Query capped at 512 chars.",
    },
    "memory_save": {
        "schema": {"note": "str", "scope": "str='workspace'"},
        "risky": False,
        "effect_class": "memory_write",
        "description": "Append an explicitly authorized note (<=1024 model tokens and 16 KiB) to agent_notes.md.",
    },
    "repo_lookup": {
        "schema": {"symbol": "str", "kind": "str=''"},
        "risky": False,
        "effect_class": "read_only",
        "description": "Look up where a symbol is defined. Precise for Python (AST), best-effort for TS/Go/Rust (regex).",
    },
}

DELEGATE_TOOL_SPEC = {
    "schema": {"task": "str", "name": "str='delegate'", "max_steps": "int=3"},
    "risky": False,
    "effect_class": "read_only",
    "description": "Ask one named, bounded read-only child agent to investigate.",
}

PLAN_TOOL_SPECS = {
    "read_plan": {
        "schema": {},
        "risky": False,
        "effect_class": "read_only",
        "description": "Read the current saved implementation plan.",
    },
    "write_plan": {
        "schema": {"plan": "str"},
        "risky": False,
        "effect_class": "session_state",
        "description": "Save the implementation plan for user review.",
    },
    "exit_plan_mode": {
        "schema": {},
        "risky": True,
        "effect_class": "session_state",
        "description": "Present the saved plan for approval and start coding.",
    },
}


def legal_tool_names():
    return set(BASE_TOOL_SPECS) | set(PLAN_TOOL_SPECS) | {"delegate"}


TOOL_EXAMPLES = {
    "list_files": '{"name":"list_files","arguments":{"path":"."}}',
    "read_file": '{"name":"read_file","arguments":{"path":"README.md","start":1,"end":80}}',
    "search": '{"name":"search","arguments":{"pattern":"binary_search","path":"."}}',
    "run_shell": f'{{"name":"run_shell","arguments":{{"command":"uv run --with pytest python -m pytest -q","timeout":{DEFAULT_RUN_SHELL_TIMEOUT}}}}}',
    "write_file": '{"name":"write_file","arguments":{"path":"binary_search.py","content":"def binary_search(nums, target):\\n    return -1\\n"}}',
    "patch_file": '{"name":"patch_file","arguments":{"path":"binary_search.py","old_text":"return -1","new_text":"return mid"}}',
    "delegate": '{"name":"delegate","arguments":{"task":"inspect README.md","name":"repo-inspector","max_steps":3}}',
    "memory_list": '{"name":"memory_list","arguments":{"prefix":"workspace/"}}',
    "memory_read": '{"name":"memory_read","arguments":{"path":"workspace/notes/auth.md","start":1,"end":200}}',
    "memory_search": '{"name":"memory_search","arguments":{"query":"bcrypt","limit":5}}',
    "memory_save": '{"name":"memory_save","arguments":{"note":"bcrypt rounds > 12 causes CI timeout"}}',
    "repo_lookup": '{"name":"repo_lookup","arguments":{"symbol":"AuthMiddleware"}}',
    "read_plan": '{"name":"read_plan","arguments":{}}',
    "write_plan": '{"name":"write_plan","arguments":{"plan":"# Plan\\n1. Inspect\\n2. Implement\\n3. Test"}}',
    "exit_plan_mode": '{"name":"exit_plan_mode","arguments":{}}',
}


def tool_delegate(context, args):
    if context.depth >= context.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")
    return context.spawn_delegate(args)


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search": tool_search,
    "run_shell": _tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "memory_list": tool_memory_list,
    "memory_read": tool_memory_read,
    "memory_search": tool_memory_search,
    "memory_save": tool_memory_save,
    "repo_lookup": tool_repo_lookup,
}


def _available_shell_executable_names(context):
    return sorted(getattr(context, "trusted_executables", {}))


def build_tool_registry(context):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
        for name, spec in BASE_TOOL_SPECS.items()
    }
    trusted_names = _available_shell_executable_names(context)
    availability = ", ".join(trusted_names) if trusted_names else "none"
    tools["run_shell"]["description"] = (
        f"{tools['run_shell']['description']} "
        f"Available trusted executable names: {availability}."
    )
    # 子 agent 是刻意做成受限能力的：一旦深度耗尽，
    # 就连 delegate 这个工具都不再暴露给模型。
    if context.depth < context.max_depth:
        tools["delegate"] = {
            **DELEGATE_TOOL_SPEC,
            "run": partial(tool_delegate, context),
        }
    for name, spec in PLAN_TOOL_SPECS.items():
        tools[name] = dict(spec)
    return tools


def tool_example(name):
    return TOOL_EXAMPLES.get(name, "")
