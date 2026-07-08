"""Memory command handlers for Pico's explicit CLI surface."""

import shutil
import time
from pathlib import Path

from .cli_errors import CLI_EXIT_USAGE, CliError
from .cli_output import print_result


def handle_memory(tokens, root, args):
    """`pico-cli memory {list | show <path> | search <query> | review | migrate}`.

    5 个只读 / 半只读子命令，把 v2 记忆系统能力暴露给用户。
    数据源：`<root>/.pico/memory/` （workspace）+ `~/.pico/memory/` （user global）。
    """
    from .memory.block_store import BlockStore

    sub = tokens[0] if tokens else ""
    rest = tokens[1:]

    workspace_memory = Path(root) / ".pico" / "memory"
    user_memory = Path.home() / ".pico" / "memory"
    store = BlockStore(workspace_root=workspace_memory, user_root=user_memory)

    if sub == "list":
        return _memory_list_cmd(store, rest, args)
    if sub == "show":
        return _memory_show_cmd(store, rest, args)
    if sub == "search":
        return _memory_search_cmd(store, rest, args)
    if sub == "review":
        return _memory_review_cmd(rest, args, root)
    if sub == "migrate":
        return _memory_migrate_cmd(rest, args, root)

    raise CliError(
        code="usage",
        message="usage: pico-cli memory {list | show <path> | search <query> | review | migrate [--apply]}",
        exit_code=CLI_EXIT_USAGE,
    )


def _memory_list_cmd(store, rest, args):
    entries = store.list()
    if not entries:
        return print_result(
            "memory_list",
            [],
            args,
            lambda _: "(no memory files yet)",
        )
    data = [
        {
            "path": e.path,
            "size_chars": e.size_chars,
            "mtime": e.mtime,
            "first_line": e.first_line,
        }
        for e in entries
    ]

    def render(items):
        lines = []
        notes = [i for i in items if "/notes/" in i["path"]]
        agents = [i for i in items if i["path"].endswith("/agent_notes.md")]
        if notes:
            lines.append("Notes (user-written, read-only for agent):")
            for entry in notes:
                lines.append(f"- {entry['path']} ({entry['size_chars']} chars)")
        if agents:
            if lines:
                lines.append("")
            lines.append("Agent records:")
            for entry in agents:
                lines.append(f"- {entry['path']} ({entry['size_chars']} chars)")
        return "\n".join(lines) or "(no memory files yet)"

    return print_result("memory_list", data, args, render)


def _memory_show_cmd(store, rest, args):
    if not rest:
        raise CliError(
            code="usage",
            message="usage: pico-cli memory show <path>",
            exit_code=CLI_EXIT_USAGE,
        )
    path = rest[0]
    try:
        content = store.read(path)
    except FileNotFoundError as exc:
        raise CliError(
            code="memory_not_found",
            message=f"memory file not found: {path}",
            hint="Run `pico-cli memory list` to see available paths.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc
    except ValueError as exc:
        raise CliError(
            code="invalid_path",
            message=str(exc),
            exit_code=CLI_EXIT_USAGE,
        ) from exc
    def render(d):
        lines = d["content"].splitlines() or [""]
        return "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines, start=1))

    return print_result(
        "memory_show",
        {"path": path, "content": content},
        args,
        render,
    )


def _memory_search_cmd(store, rest, args):
    from .memory.retrieval import Retrieval

    if not rest:
        raise CliError(
            code="usage",
            message="usage: pico-cli memory search <query> [--limit N]",
            exit_code=CLI_EXIT_USAGE,
        )
    query = rest[0]
    limit = 5
    if "--limit" in rest:
        idx = rest.index("--limit")
        if idx + 1 < len(rest):
            try:
                limit = max(1, min(int(rest[idx + 1]), 20))
            except ValueError:
                raise CliError(
                    code="usage",
                    message="--limit requires an integer",
                    exit_code=CLI_EXIT_USAGE,
                )
    retrieval = Retrieval(store)
    hits = retrieval.search(query, limit=limit)
    data = [
        {"path": h.path, "score": h.score, "snippets": list(h.snippets)}
        for h in hits
    ]

    def render(items):
        if not items:
            return f"No matches for {query!r}."
        lines = [f"Found {len(items)} match(es) for {query!r}:"]
        for hit in items:
            lines.append(f"- {hit['path']} (score={hit['score']:.2f})")
            for snip in hit["snippets"][:2]:
                lines.append(f"  {snip}")
        return "\n".join(lines)

    return print_result("memory_search", data, args, render)


def _memory_review_cmd(rest, args, root):
    """展示 agent_notes.md 内容 + 使用提示。用户自己决定要不要 vim/rm。"""
    agent_notes = Path(root) / ".pico" / "memory" / "agent_notes.md"
    if not agent_notes.exists():
        return print_result(
            "memory_review",
            {"exists": False},
            args,
            lambda _: "(no agent_notes.md yet — empty)",
        )
    content = agent_notes.read_text(encoding="utf-8")
    data = {"exists": True, "chars": len(content), "content": content}

    def render(d):
        return (
            f"agent_notes.md ({d['chars']} chars):\n\n"
            f"{d['content']}\n"
            f"To edit: vim .pico/memory/agent_notes.md\n"
            f"To clear: rm .pico/memory/agent_notes.md"
        )

    return print_result("memory_review", data, args, render)


def _memory_migrate_cmd(rest, args, root):
    """Preview or apply migration from legacy .pico/memory/topics/*.md to notes/*.md."""
    apply_flag = "--apply" in rest
    topics_dir = Path(root) / ".pico" / "memory" / "topics"
    notes_dir = Path(root) / ".pico" / "memory" / "notes"

    if not topics_dir.exists():
        return print_result(
            "memory_migrate",
            {"applied": False, "reason": "no_legacy_topics"},
            args,
            lambda _: "(no legacy .pico/memory/topics/ found — nothing to migrate)",
        )

    old_files = sorted(
        p
        for p in topics_dir.glob("*.md")
        if p.is_file() and not p.name.endswith(".deprecated")
    )
    if not old_files:
        return print_result(
            "memory_migrate",
            {"applied": False, "reason": "empty"},
            args,
            lambda _: "(legacy topics/ empty — nothing to migrate)",
        )

    plan = []
    for src in old_files:
        dst = notes_dir / src.name
        plan.append(
            {
                "src": str(src.relative_to(Path(root))),
                "dst": str(dst.relative_to(Path(root))),
                "conflict": dst.exists(),
            }
        )

    if not apply_flag:
        def render_plan(items):
            lines = ["Migration preview (use --apply to actually migrate):"]
            for entry in items:
                marker = "  [CONFLICT: destination exists, will skip]" if entry["conflict"] else ""
                lines.append(f"- would copy {entry['src']} -> {entry['dst']}{marker}")
            lines.append("")
            lines.append("Legacy files will be renamed to *.deprecated (not deleted).")
            return "\n".join(lines)

        return print_result("memory_migrate", plan, args, render_plan)

    notes_dir.mkdir(parents=True, exist_ok=True)
    migrated = []
    for entry in plan:
        src = Path(root) / entry["src"]
        dst = Path(root) / entry["dst"]
        if dst.exists():
            continue
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        deprecated = src.with_suffix(src.suffix + ".deprecated")
        src.rename(deprecated)
        migrated.append(entry)

    def render(items):
        if not items:
            return "(nothing migrated — all destinations already exist)"
        lines = ["Migrated:"]
        for entry in items:
            lines.append(f"- {entry['src']} -> {entry['dst']}")
        lines.append("")
        lines.append("Legacy files renamed to *.deprecated. Delete manually when ready.")
        return "\n".join(lines)

    return print_result("memory_migrate", migrated, args, render)


# ---- Task 22: agent_notes.md → agent/legacy-import.md migrator ---------------
# The older `_memory_migrate_cmd` above handles a *different* legacy path
# (`.pico/memory/topics/*.md` → `.pico/memory/notes/*.md`) from an earlier plan.
# This module-level function serves the new per-topic migration: it takes the
# legacy single-file `agent_notes.md`, wraps its contents in frontmatter, moves
# it to `agent/legacy-import.md`, and renames the source with a `.legacy`
# suffix so retrieval skips it. `--dry-run` and `--rollback` are supported.

def cli_memory_migrate(workspace_root, *, dry_run: bool = False, rollback: bool = False) -> int:
    """Migrate `agent_notes.md` into `agent/legacy-import.md`.

    Parameters
    ----------
    workspace_root
        Directory that contains ``agent_notes.md`` (typically
        ``<repo>/.pico/memory``).
    dry_run
        If True, print the planned actions but touch no files. Returns 0.
    rollback
        If True, undo a previous migrate: restore ``agent_notes.md`` from the
        ``.legacy`` sibling and delete ``agent/legacy-import.md``. Returns 0
        on success, 1 when there is nothing to roll back.
    """
    ws = Path(workspace_root)
    legacy_target = ws / "agent" / "legacy-import.md"
    old_notes = ws / "agent_notes.md"
    renamed = ws / "agent_notes.md.legacy"
    backup_dir = ws / "backup"

    if rollback:
        if not renamed.exists():
            print("nothing to rollback (agent_notes.md.legacy not found)")
            return 1
        if legacy_target.exists():
            if dry_run:
                print(f"[dry-run] would delete {legacy_target}")
            else:
                legacy_target.unlink()
        if dry_run:
            print(f"[dry-run] would rename {renamed} → {old_notes}")
            return 0
        renamed.rename(old_notes)
        print(f"rolled back: {old_notes}")
        return 0

    if not old_notes.exists():
        print("no agent_notes.md to migrate")
        return 0

    body = old_notes.read_text(encoding="utf-8")
    ts = int(time.time())

    if dry_run:
        print(f"[dry-run] would backup {old_notes} → {backup_dir / f'agent_notes.md.{ts}'}")
        print(f"[dry-run] would create {legacy_target} with legacy frontmatter")
        print(f"[dry-run] would rename {old_notes} → {renamed}")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(old_notes, backup_dir / f"agent_notes.md.{ts}")

    legacy_target.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        "name: legacy-import\n"
        "type: feedback\n"
        "description: Migrated legacy agent notes\n"
        "tags: [legacy]\n"
        "aliases: []\n"
        "supersedes: []\n"
        "---\n\n"
    )
    legacy_target.write_text(fm + body, encoding="utf-8")
    old_notes.rename(renamed)
    print(f"migrated to {legacy_target}")
    return 0
