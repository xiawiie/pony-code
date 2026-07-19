"""Explicit inspection, merge, and cleanup for worktree child agents."""

from pathlib import Path

from pony.runtime.worktree_agents import (
    cleanup_worktree_agent,
    inspect_worktree_agent,
    list_worktree_agents,
    merge_worktree_agent,
)
from pony.state.file_lock import locked_file
from pony.tools.subprocess import build_trusted_executables

from .errors import CLI_EXIT_RUNTIME, CLI_EXIT_USAGE, CliError
from .output import print_inspection_result


def _git(root):
    executable = build_trusted_executables(root).get("git")
    if not executable:
        raise CliError(
            code="git_unavailable",
            message="trusted Git executable is unavailable",
            exit_code=CLI_EXIT_RUNTIME,
        )
    return executable


def _render_list(items):
    if not items:
        return "no worktree agents"
    return "\n".join(
        f"{item['id']}  {item['status']}  {item['branch']}" for item in items
    )


def _render_show(item):
    return "\n".join(
        (
            f"agent: {item['id']}",
            f"name: {item['name']}",
            f"status: {item['status']}",
            f"mode: {item['mode']}",
            f"branch: {item['branch']}",
            f"worktree: {item['worktree']}",
            f"base: {item['base_commit']}",
            f"diff: {item.get('diff_status', 'unknown')}",
            f"changed_files: {item['changed_files']}",
            f"tests: {item['test_status']}",
        )
    )


def _usage():
    raise CliError(
        code="usage",
        message="usage: pony agents {list|show|merge|cleanup} [agent-id]",
        exit_code=CLI_EXIT_USAGE,
    )


def handle_agents(tokens, root, args):
    root = Path(root).resolve(strict=True)
    tokens = list(tokens)
    try:
        if tokens == ["list"]:
            items = list_worktree_agents(root)
            return print_inspection_result(
                root,
                "worktree_agents",
                items,
                args,
                _render_list,
            )
        if len(tokens) != 2 or tokens[0] not in {"show", "merge", "cleanup"}:
            return _usage()
        command, agent_id = tokens
        git = _git(root)
        if command == "show":
            item = inspect_worktree_agent(root, agent_id, git)
        else:
            with locked_file(root / ".pony" / ".workspace-mutation.lock", require_lock=True):
                item = (
                    merge_worktree_agent(root, agent_id, git)
                    if command == "merge"
                    else cleanup_worktree_agent(root, agent_id, git)
                )
            item["worktree"] = str(root / item["worktree_rel"])
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise CliError(
            code="worktree_agent_error",
            message=str(exc)[:300] or "worktree agent operation failed",
            exit_code=CLI_EXIT_RUNTIME,
        ) from exc
    return print_inspection_result(
        root,
        f"worktree_agent_{command}",
        item,
        args,
        _render_show,
    )
