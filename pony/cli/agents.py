"""Explicit inspection, merge, and cleanup for worktree child agents."""

from pathlib import Path

from pony.security.private_files import private_directory_identity
from pony.runtime.worktree_agents import (
    cleanup_worktree_agent,
    inspect_worktree_agent,
    inspect_worktree_agent_batch,
    list_worktree_agent_batches,
    list_worktree_agents,
    merge_worktree_agent_batch,
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
    lines = [
        f"agent: {item['id']}",
        f"name: {item['name']}",
        f"status: {item['status']}",
        f"mode: {item['mode']}",
        f"branch: {item['branch']}",
        f"worktree: {item['worktree']}",
        f"base: {item['base_commit']}",
        f"diff: {item.get('diff_status', 'unknown')}",
        f"changed_files: {item['changed_files']}",
    ]
    if "worktree_diff_status" in item:
        lines.extend(
            (
                f"worktree_diff_status: {item['worktree_diff_status']}",
                f"worktree_changed_files: {item['worktree_changed_files']}",
            )
        )
    lines.append(f"tests: {item['test_status']}")
    return "\n".join(lines)


def _render_batches(items):
    if not items:
        return "no worktree agent batches"
    return "\n".join(
        f"{item['id']}  {item['status']}  {len(item['children'])} children"
        for item in items
    )


def _render_batch(item):
    lines = [
        f"batch: {item['id']}",
        f"status: {item['status']}",
        f"base: {item['base_commit']}",
    ]
    for child in item["children"]:
        review = (
            " review_required/untested"
            if child["changed_files"] and child["test_status"] == "not_run"
            else ""
        )
        lines.append(
            f"- {child['id']}: {child['status']}; "
            f"tests={child['test_status']}; files={child['changed_files']}{review}"
        )
        if child.get("sealed_evidence_matches") is False:
            lines.append("  sealed_evidence: mismatch")
    overlaps = item.get("overlapping_paths", {})
    lines.append(f"overlapping_paths: {len(overlaps)}")
    for path, agent_ids in overlaps.items():
        lines.append(f"  {path}: {', '.join(agent_ids)}")
    return "\n".join(lines)


def _usage():
    raise CliError(
        code="usage",
        message=(
            "usage: pony agents {list|batches|show|show-batch|merge|merge-all|cleanup} "
            "[id] [--discard]"
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
        if tokens == ["batches"]:
            items = list_worktree_agent_batches(root)
            return print_inspection_result(
                root,
                "worktree_agent_batches",
                items,
                args,
                _render_batches,
            )
        if tokens[0:1] == ["cleanup"] and len(tokens) == 3:
            command, agent_id, discard_flag = tokens
            if discard_flag != "--discard":
                return _usage()
            discard = True
        elif len(tokens) == 2 and tokens[0] in {
            "show",
            "show-batch",
            "merge",
            "merge-all",
            "cleanup",
        }:
            command, agent_id = tokens
            discard = False
        else:
            return _usage()
        git = _git(root)
        if command == "show":
            item = inspect_worktree_agent(root, agent_id, git)
        elif command == "show-batch":
            item = inspect_worktree_agent_batch(root, agent_id, git)
        else:
            with locked_file(root / ".pony" / ".workspace-mutation.lock", require_lock=True):
                _require_mutation_trust(root, expected_root_identity, trust_store)
                item = (
                    merge_worktree_agent_batch(root, agent_id, git)
                    if command == "merge-all"
                    else (
                        merge_worktree_agent(root, agent_id, git)
                        if command == "merge"
                        else cleanup_worktree_agent(
                            root, agent_id, git, discard=discard
                        )
                    )
                )
            if "worktree_rel" in item:
                item["worktree"] = str(root / item["worktree_rel"])
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise CliError(
            code="worktree_agent_error",
            message=str(exc)[:300] or "worktree agent operation failed",
            exit_code=CLI_EXIT_RUNTIME,
        ) from exc
    kind = {
        "show-batch": "worktree_agent_show_batch",
        "merge-all": "worktree_agent_merge_all",
    }.get(command, f"worktree_agent_{command}")
    return print_inspection_result(
        root,
        kind,
        item,
        args,
        _render_batch if command in {"show-batch", "merge-all"} else _render_show,
    )
