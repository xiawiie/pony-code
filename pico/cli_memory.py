"""Memory command handlers for Pico's explicit CLI surface."""

from pathlib import Path

from .cli_errors import CLI_EXIT_CONFIG, CLI_EXIT_USAGE, CliError
from .cli_output import print_result


def handle_memory(tokens, root, args):
    """`pico-cli memory {list | show <path> | search <query> | review}`.

    4 个只读 / 半只读子命令，把 v2 记忆系统能力暴露给用户。
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
        return _memory_review_cmd(store, rest, args)

    raise CliError(
        code="usage",
        message="usage: pico-cli memory {list | show <path> | search <query> | review}",
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


def _memory_review_cmd(store, rest, args):
    if rest:
        raise CliError(
            code="usage",
            message="usage: pico-cli memory review",
            exit_code=CLI_EXIT_USAGE,
        )
    try:
        content = store.read("workspace/agent_notes.md")
    except FileNotFoundError:
        return print_result(
            "memory_review",
            {"exists": False},
            args,
            lambda _: "(no agent_notes.md yet — empty)",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CliError(
            code="memory_unavailable",
            message="agent notes could not be read safely",
            exit_code=CLI_EXIT_CONFIG,
        ) from exc

    data = {"exists": True, "chars": len(content), "content": content}

    def render(item):
        return (
            f"agent_notes.md ({item['chars']} chars):\n\n"
            f"{item['content']}\n"
            "To edit: vim .pico/memory/agent_notes.md\n"
            "To clear: rm .pico/memory/agent_notes.md"
        )

    return print_result("memory_review", data, args, render)
