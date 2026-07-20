"""Explicit inspection, merge, and cleanup for worktree child agents."""

from pathlib import Path

from pony.security.private_files import private_directory_identity
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
        message=(
            "usage: pony agents {list|show|merge|cleanup} [agent-id] "
            "[--discard]"
        ),
        exit_code=CLI_EXIT_USAGE,
    )


def _require_mutation_trust(root, expected_root_identity, trust_store):
    if (
        expected_root_identity is None
        or trust_store is None
        or private_directory_identity(root) != expected_root_identity
        or not trust_store.is_trusted(root)
    ):
        raise ValueError("project trust changed")


def handle_agents(
    tokens,
    root,
    args,
    *,
    expected_root_identity=None,
    trust_store=None,
):
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
        if tokens[0:1] == ["cleanup"] and len(tokens) == 3:
            command, agent_id, discard_flag = tokens
            if discard_flag != "--discard":
                return _usage()
            discard = True
        elif len(tokens) == 2 and tokens[0] in {"show", "merge", "cleanup"}:
            command, agent_id = tokens
            discard = False
        else:
            return _usage()
        git = _git(root)
        if command == "show":
            item = inspect_worktree_agent(root, agent_id, git)
        else:
            with locked_file(root / ".pony" / ".workspace-mutation.lock", require_lock=True):
                _require_mutation_trust(root, expected_root_identity, trust_store)
                item = (
                    merge_worktree_agent(root, agent_id, git)
                    if command == "merge"
                    else cleanup_worktree_agent(root, agent_id, git, discard=discard)
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
