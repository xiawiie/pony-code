"""Command handlers for Pico's explicit CLI Surface."""

import getpass
import sys
from pathlib import Path

from .cli_errors import CLI_EXIT_USAGE, CliError
from .cli_diagnostics import _line
from .cli_diagnostics import handle_config, handle_doctor, handle_status  # noqa: F401
from .cli_output import print_result
from .cli_recovery import handle_checkpoints, handle_runs, handle_sessions  # noqa: F401
from .config import _parse_env_line
from .providers.defaults import (
    API_KEY_ENV_NAMES,
    BASE_URL_ENV_NAMES,
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    DEFAULT_PROVIDER,
    MODEL_ENV_NAMES,
    PROVIDER_CHOICES,
)
from .workspace import WorkspaceContext


ROOT_HELP = """pico-cli — Local coding agent for repository-grounded engineering work.

USAGE:
    pico-cli <command> [subcommand] [options]
    pico-cli run <prompt...>

EXAMPLES:
    pico-cli run "inspect the failing tests"
    pico-cli doctor
    pico-cli checkpoints preview-restore <checkpoint-id>

Available Commands:
  run          Run one prompt and exit
  repl         Start interactive REPL
  status       Show local workspace state
  doctor       Check config, storage, auth, and connectivity
  init         Create or update project .env provider config
  config       Configuration inspection
  runs         Run artifact inspection
  sessions     Session inspection
  checkpoints  Checkpoint recovery inspection
  memory       Memory files inspection & migration
  help         Help about any command

Flags:
  -h, --help       help for pico-cli
      --format     output format for inspection commands: text or json
      --quiet      suppress non-essential human output

Compatibility:
    pico-cli "prompt"      Run a one-shot prompt
    pico                   Legacy entry point; may conflict with /usr/bin/pico
"""


def handle_help(tokens):
    print(ROOT_HELP.rstrip())
    return 0


def handle_init(tokens, cwd, args):
    options = _parse_init_tokens(tokens)
    workspace = WorkspaceContext.build(cwd)
    env_path = Path(workspace.repo_root) / ".env"
    existing = _read_env_assignments(env_path)
    provider = options["provider"] or getattr(args, "provider", None) or existing.get("PICO_PROVIDER") or DEFAULT_PROVIDER
    if provider not in PROVIDER_CHOICES:
        raise CliError(
            code="usage",
            message=f"unknown provider: {provider}",
            hint=f"Expected one of: {', '.join(PROVIDER_CHOICES)}.",
            exit_code=CLI_EXIT_USAGE,
        )

    assignments = {"PICO_PROVIDER": provider}
    model_name = _primary_env_name(MODEL_ENV_NAMES, provider)
    base_url_name = _primary_env_name(BASE_URL_ENV_NAMES, provider)
    api_key_name = _primary_env_name(API_KEY_ENV_NAMES, provider)

    if model_name:
        assignments[model_name] = (
            options["model"]
            or getattr(args, "model", None)
            or existing.get(model_name)
            or DEFAULT_MODELS.get(provider, "")
        )
    if base_url_name:
        assignments[base_url_name] = (
            options["base_url"]
            or getattr(args, "base_url", None)
            or _host_override(args, provider)
            or existing.get(base_url_name)
            or DEFAULT_BASE_URLS.get(provider, "")
        )

    api_key_value = ""
    if api_key_name:
        api_key_value = options["api_key"]
        if api_key_value is None:
            api_key_value = existing.get(api_key_name)
        if api_key_value is None:
            api_key_value = _prompt_api_key(provider, args)
        assignments[api_key_name] = api_key_value or ""

    written = _write_env_assignments(env_path, assignments)
    data = {
        "env_path": str(env_path),
        "provider": provider,
        "updated": written["updated"],
        "added": written["added"],
        "unchanged": written["unchanged"],
        "api_key": {
            "present": bool(api_key_value),
            "name": api_key_name,
        },
    }
    return print_result("config_init", data, args, _render_init)


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


def run_agent_once(agent, prompt_tokens):
    prompt = " ".join(prompt_tokens).strip()
    if not prompt:
        return 0
    print()
    try:
        print(agent.ask(prompt))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def run_repl(agent):
    while True:
        try:
            user_input = input("\npico> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            from .cli_help import HELP_DETAILS

            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            task_summary = agent.memory.task_summary
            recent_files = agent.memory.recent_files
            print(f"task: {task_summary or '(empty)'}")
            print(f"recent: {', '.join(recent_files) if recent_files else '(empty)'}")
            try:
                entries = agent.memory_store.list()
            except Exception:  # noqa: BLE001 — REPL loop must never crash on a listing failure
                entries = []
            if entries:
                print("\nMemory files:")
                for entry in entries:
                    print(f"- {entry.path} ({entry.size_chars} chars)")
            else:
                print("\nMemory files: (none — use /save <text> or edit .pico/memory/notes/*.md)")
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue
        if user_input.startswith("/save"):
            note = user_input[len("/save"):].strip()
            if not note:
                print("usage: /save <text>")
                continue
            try:
                total = agent.memory_store.append_agent_note(scope="workspace", note=note)
            except ValueError as exc:
                print(f"error: {exc}")
                continue
            print(f"saved (chars_total={total})")
            continue
        if user_input == "/memory-review":
            notes_path = Path(agent.root) / ".pico" / "memory" / "agent_notes.md"
            if notes_path.exists():
                content = notes_path.read_text(encoding="utf-8")
                print(f"agent_notes.md ({len(content)} chars):\n\n{content}")
                print("To edit: vim .pico/memory/agent_notes.md")
            else:
                print("(no agent_notes.md yet)")
            continue

        print()
        try:
            print(agent.ask(user_input))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)


def _render_init(data):
    api_key = data["api_key"]
    if api_key["name"]:
        api_key_text = f"{'present' if api_key['present'] else 'missing'} ({api_key['name']})"
    else:
        api_key_text = "not required"
    changed = [*data["updated"], *data["added"]]
    lines = [
        "Pico init — Project .env configured",
        "",
        _line("env file", data["env_path"]),
        _line("provider", data["provider"]),
        _line("api key", api_key_text),
        _line("updated", ", ".join(changed) if changed else "-"),
    ]
    return "\n".join(lines)


def _parse_init_tokens(tokens):
    options = {
        "provider": None,
        "model": None,
        "base_url": None,
        "api_key": None,
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"--provider", "--model", "--base-url", "--api-key"}:
            if index + 1 >= len(tokens):
                raise _init_usage_error()
            key = token[2:].replace("-", "_")
            options[key] = tokens[index + 1]
            index += 2
            continue
        for flag in ("--provider=", "--model=", "--base-url=", "--api-key="):
            if token.startswith(flag):
                key = flag[2:-1].replace("-", "_")
                options[key] = token[len(flag):]
                break
        else:
            raise _init_usage_error()
        index += 1
    return options


def _init_usage_error():
    return CliError(
        code="usage",
        message="usage: pico-cli init [--provider <name>] [--model <name>] [--base-url <url>] [--api-key <key>]",
        exit_code=CLI_EXIT_USAGE,
    )


def _primary_env_name(mapping, provider):
    names = mapping.get(provider, ())
    return names[0] if names else ""


def _host_override(args, provider):
    if provider != "ollama":
        return None
    host = getattr(args, "host", None)
    if host and host != DEFAULT_BASE_URLS.get("ollama"):
        return host
    return None


def _prompt_api_key(provider, args):
    if getattr(args, "no_input", False) or not sys.stdin.isatty():
        return ""
    try:
        return getpass.getpass(f"{provider} API key (leave blank to fill later): ")
    except (EOFError, KeyboardInterrupt):
        return ""


def _read_env_assignments(env_path):
    if not env_path.exists():
        return {}
    assignments = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        name, value = parsed
        assignments[name] = value
    return assignments


def _write_env_assignments(env_path, assignments):
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = dict(assignments)
    rendered = []
    updated = []
    unchanged = []
    for line in existing_lines:
        parsed = _parse_env_line(line)
        if parsed is None:
            rendered.append(line)
            continue
        name, old_value = parsed
        if name not in remaining:
            rendered.append(line)
            continue
        value = remaining.pop(name)
        rendered.append(_format_env_assignment(name, value))
        if old_value == value:
            unchanged.append(name)
        else:
            updated.append(name)

    added = list(remaining)
    if remaining:
        if rendered and rendered[-1].strip():
            rendered.append("")
        for name, value in remaining.items():
            rendered.append(_format_env_assignment(name, value))

    env_path.write_text("\n".join(rendered).rstrip() + "\n", encoding="utf-8")
    return {"updated": updated, "added": added, "unchanged": unchanged}


def _format_env_assignment(name, value):
    text = str(value or "")
    if "\n" in text or "\r" in text:
        raise CliError(
            code="usage",
            message=f"{name} cannot contain newlines",
            exit_code=CLI_EXIT_USAGE,
        )
    return f"{name}={text}"
