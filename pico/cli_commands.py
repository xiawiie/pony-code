"""Command handlers for Pico's explicit CLI Surface."""

import getpass
import json
import sys
from pathlib import Path

from .checkpoint_store import CheckpointStore
from .cli_errors import CLI_EXIT_USAGE, CliError
from .cli_diagnostics import collect_config, collect_doctor, collect_status
from .cli_output import format_json, success_envelope
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
from .recovery_checkpoint_writer import RecoveryCheckpointWriter
from .recovery_manager import RecoveryManager
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


def print_result(kind, data, args, text_renderer):
    if getattr(args, "format", "text") == "json":
        print(format_json(success_envelope(kind, data)), end="")
        return 0

    text = text_renderer(data)
    if text and not getattr(args, "quiet", False):
        print(text, end="" if text.endswith("\n") else "\n")
    return 0


def handle_help(tokens):
    print(ROOT_HELP.rstrip())
    return 0


def handle_checkpoints(root, tokens, args):
    store = CheckpointStore(root)
    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    if sub == "list" and not rest:
        records = store.list_checkpoint_records()
        return print_result("checkpoints_list", records, args, _render_checkpoints_list)
    if sub == "show" and len(rest) == 1:
        checkpoint_id = _resolve_checkpoint_id(store, rest[0])
        record = _load_checkpoint_record(store, checkpoint_id)
        return print_result("checkpoints_show", record, args, _render_json_body)
    if sub == "preview-restore" and len(rest) == 1:
        manager = RecoveryManager(store, root, checkpoint_writer=RecoveryCheckpointWriter(store, root))
        checkpoint_id = _resolve_checkpoint_id(store, rest[0])
        plan = _preview_restore(manager, checkpoint_id)
        return print_result("checkpoints_preview_restore", plan, args, _render_restore_plan)
    if sub == "restore" and _is_restore_args(rest):
        checkpoint_id = _resolve_checkpoint_id(store, rest[0])
        apply_flag = "--apply" in rest[1:]
        manager = RecoveryManager(store, root, checkpoint_writer=RecoveryCheckpointWriter(store, root))
        if not apply_flag:
            plan = _preview_restore(manager, checkpoint_id)
            return print_result("checkpoints_preview_restore", plan, args, _render_restore_plan)
        result = _apply_restore(manager, checkpoint_id)
        return print_result("checkpoints_restore", result, args, _render_json_body)
    if sub == "prune":
        prune_options = _parse_prune_args(rest)
        try:
            result = store.prune(
                dry_run=not prune_options["apply"],
                older_than=prune_options["older_than"],
            )
        except ValueError as exc:
            raise CliError(
                code="usage",
                message=str(exc),
                exit_code=CLI_EXIT_USAGE,
            ) from exc
        return print_result("checkpoints_prune", result, args, _render_json_body)
    raise CliError(
        code="usage",
        message="usage: pico-cli checkpoints {list | show <id> | preview-restore <id> | restore <id> [--apply] | prune [--older-than <duration>] [--apply]}",
        exit_code=CLI_EXIT_USAGE,
    )


def handle_runs(root, tokens, args):
    runs_root = Path(root) / ".pico" / "runs"
    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    if sub == "list" and not rest:
        data = []
        if runs_root.exists():
            data = [{"run_id": entry.name} for entry in sorted(runs_root.iterdir()) if entry.is_dir()]
        return print_result("runs_list", data, args, _render_runs_list)
    if sub == "show" and len(rest) == 1:
        run_dir = runs_root / rest[0]
        if not run_dir.exists():
            raise CliError(
                code="run_not_found",
                message=f"unknown run: {rest[0]}",
                hint="Run `pico-cli runs list`.",
                exit_code=CLI_EXIT_USAGE,
            )
        data = _load_run_artifacts(run_dir, rest[0])
        return print_result("runs_show", data, args, _render_runs_show)
    raise CliError(
        code="usage",
        message="usage: pico-cli runs {list | show <run_id>}",
        exit_code=CLI_EXIT_USAGE,
    )


def handle_status(cwd, args):
    return print_result("status", collect_status(cwd, args), args, _render_status)


def handle_doctor(tokens, cwd, args):
    offline = False
    if tokens == ["--offline"]:
        offline = True
    elif tokens:
        raise CliError(
            code="usage",
            message="usage: pico-cli doctor [--offline]",
            exit_code=CLI_EXIT_USAGE,
        )
    return print_result("doctor", collect_doctor(cwd, args, offline=offline), args, _render_doctor)


def handle_config(tokens, cwd, args):
    sub = tokens[0] if tokens else ""
    rest = tokens[1:]
    if sub == "show" and not rest:
        return print_result(
            "config_show",
            collect_config(cwd, args),
            args,
            _render_config,
        )
    raise CliError(
        code="usage",
        message="usage: pico-cli config show",
        exit_code=CLI_EXIT_USAGE,
    )


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


def handle_sessions(root, tokens, args):
    sessions_root = Path(root) / ".pico" / "sessions"
    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    if sub == "list" and not rest:
        data = [{"session_id": path.stem} for path in _session_files(sessions_root)]
        return print_result("sessions_list", data, args, _render_sessions_list)
    if sub == "show" and len(rest) == 1:
        session_id = rest[0]
        session_paths = {path.stem: path for path in _session_files(sessions_root)}
        path = session_paths.get(session_id)
        if path is None:
            raise CliError(
                code="session_not_found",
                message=f"unknown session: {session_id}",
                hint="Run `pico-cli sessions list`.",
                exit_code=CLI_EXIT_USAGE,
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return print_result("sessions_show", data, args, _render_json_body)
    raise CliError(
        code="usage",
        message="usage: pico-cli sessions {list | show <session_id>}",
        exit_code=CLI_EXIT_USAGE,
    )


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


def _render_checkpoints_list(records):
    lines = []
    for record in records:
        lines.append(f"{record['checkpoint_id']}\t{record['checkpoint_type']}\t{record.get('created_at', '')}")
    return "\n".join(lines)


def _is_restore_args(args):
    return len(args) == 1 or (len(args) == 2 and args[1] == "--apply")


def _parse_prune_args(args):
    options = {"apply": False, "older_than": None}
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--apply":
            options["apply"] = True
            index += 1
            continue
        if token == "--older-than":
            if index + 1 >= len(args):
                raise _prune_usage_error()
            options["older_than"] = args[index + 1]
            index += 2
            continue
        if token.startswith("--older-than="):
            options["older_than"] = token.split("=", 1)[1]
            index += 1
            continue
        raise _prune_usage_error()
    return options


def _prune_usage_error():
    return CliError(
        code="usage",
        message="usage: pico-cli checkpoints prune [--older-than <duration>] [--apply]",
        exit_code=CLI_EXIT_USAGE,
    )


def _render_json_body(data):
    return json.dumps(data, indent=2, sort_keys=True)


def _render_restore_plan(plan):
    entries = list(plan.get("entries", []) or [])
    count = len(entries)
    noun = "entry" if count == 1 else "entries"
    lines = [
        f"Restore plan {plan.get('checkpoint_id', '-')} ({count} {noun})",
        "",
        "decision  path                              reason",
    ]
    for entry in entries:
        decision = str(entry.get("decision", "-") or "-")
        path = str(entry.get("path", "-") or "-")
        reason = str(entry.get("recovery_note", "") or entry.get("reason", "") or entry.get("change_kind", "") or "-")
        observed = str(entry.get("observed_current_hash", "") or "")
        expected = str(entry.get("expected_current_hash", "") or "")
        details = reason
        if observed:
            details += f" observed={observed[:12]}"
        if expected and decision == "conflict":
            details += f" expected={expected[:12]}"
        lines.append(f"{decision:<8}  {path:<32}  {details}")
    return "\n".join(lines)


def _source_label(item):
    source = item.get("source", "")
    name = item.get("name", "")
    if source and name:
        return f"{source}:{name}"
    return source or name or "-"


def _line(label, value):
    lines = str(value).splitlines() or [""]
    rendered = [f"  {label:<14} {lines[0]}"]
    rendered.extend(f"  {'':<14} {line}" for line in lines[1:])
    return "\n".join(rendered)


def _presence_text(item):
    state = "present" if item.get("present") else "missing"
    return f"{state} ({_source_label(item)})"


def _value_with_source(item):
    return f"{item.get('value', '-') or '-'} ({_source_label(item)})"


def _ok_missing(value):
    if isinstance(value, bool):
        return "ok" if value else "missing"
    return str(value)


def _render_config(data):
    lines = [
        "Pico config — Effective configuration",
        "",
        "Provider",
        _line("provider", _value_with_source(data["provider"])),
        _line("model", _value_with_source(data["model"])),
        "",
        "Credentials",
        _line("api key", _presence_text(data["api_key"])),
    ]
    return "\n".join(lines)


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


def _render_doctor(data):
    config = data["config"]
    credentials = data["credentials"]
    connectivity = data["provider_connectivity"]
    storage = data["storage"]
    lines = [
        "Pico doctor — CLI health check",
        "",
        "Workspace",
        _line("repo root", data["workspace"]["repo_root"]),
        _line("status", data["workspace"]["status"]),
        "",
        "Config",
        _line("provider", _value_with_source(config["provider"])),
        _line("model", _value_with_source(config["model"])),
        _line("base url", _value_with_source(config["base_url"])),
        "",
        "Credentials",
        _line("api key", _presence_text(credentials["api_key"])),
        _line("status", credentials["status"]),
        "",
        "Storage",
        _line("sessions", storage["sessions"]),
        _line("runs", storage["runs"]),
        _line("checkpoints", storage["checkpoints"]),
        _line("recovery", data["recovery_store"]),
        "",
        "Provider connectivity",
        _line("status", connectivity.get("status", "-")),
    ]
    if connectivity.get("http_status") is not None:
        lines.append(_line("http", connectivity["http_status"]))
    if connectivity.get("url"):
        lines.append(_line("url", connectivity["url"]))
    if connectivity.get("message"):
        lines.append(_line("message", connectivity["message"]))
    hints = ((data.get("project_docs") or {}).get("hints")) or []
    if hints:
        lines.append("")
        lines.append("Project docs")
        for hint in hints:
            level = hint.get("level", "info")
            message = hint.get("message", "")
            lines.append(_line(level, message))
    return "\n".join(lines)


def _render_runs_list(runs):
    return "\n".join(run["run_id"] for run in runs)


def _render_sessions_list(sessions):
    return "\n".join(session["session_id"] for session in sessions)


def _session_files(sessions_root):
    if not sessions_root.exists():
        return []
    return [
        path
        for path in sorted(sessions_root.glob("*.json"))
        if path.is_file()
    ]


def _render_status(data):
    lines = [
        "Pico status — Local harness state",
        "",
        "Workspace",
        _line("repo root", data["workspace"]["repo_root"]),
        _line("cwd", data["workspace"]["cwd"]),
        _line("branch", data["workspace"]["branch"]),
        _line("git status", data["workspace"]["status"]),
        "",
        "Provider",
        _line("provider", _value_with_source(data["provider"]["provider"])),
        _line("model", _value_with_source(data["provider"]["model"])),
        _line("api key", _presence_text(data["provider"]["api_key"])),
        "",
        "Storage",
        _line("sessions", _ok_missing(data["storage"]["sessions"])),
        _line("runs", _ok_missing(data["storage"]["runs"])),
        _line("checkpoints", _ok_missing(data["storage"]["checkpoints"])),
        "",
        "Latest",
        _line("session id", data["latest"]["session_id"] or "-"),
        _line("run id", data["latest"]["run_id"] or "-"),
        _line("checkpoint id", data["latest"]["checkpoint_id"] or "-"),
    ]
    return "\n".join(lines)


def _render_runs_show(data):
    sections = []
    for artifact in data["artifacts"]:
        sections.append(f"--- {artifact['name']} ---\n{artifact['content']}")
    return "\n".join(sections)


def _load_run_artifacts(run_dir, run_id):
    artifacts = []
    for name in ("task_state.json", "report.json", "trace.jsonl"):
        path = run_dir / name
        if path.exists():
            artifacts.append({"name": name, "content": path.read_text(encoding="utf-8")})
    return {"run_id": run_id, "artifacts": artifacts}


def _resolve_checkpoint_id(store, value):
    checkpoint_id = str(value or "").strip()
    if not checkpoint_id:
        raise CliError(
            code="checkpoint_not_found",
            message="unknown checkpoint: ",
            hint="Run `pico-cli checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        )

    records = store.list_checkpoint_records()
    ids = [str(record.get("checkpoint_id", "")) for record in records if str(record.get("checkpoint_id", ""))]
    if checkpoint_id in ids:
        return checkpoint_id

    matches = [item for item in ids if item.startswith(checkpoint_id)]
    if len(matches) == 1 and len(checkpoint_id) >= 6:
        return matches[0]
    if len(matches) > 1:
        raise CliError(
            code="checkpoint_prefix_ambiguous",
            message=f"ambiguous checkpoint prefix: {checkpoint_id}",
            hint="Use a longer checkpoint id prefix.",
            exit_code=CLI_EXIT_USAGE,
            details={"candidates": matches},
        )
    raise CliError(
        code="checkpoint_not_found",
        message=f"unknown checkpoint: {checkpoint_id}",
        hint="Run `pico-cli checkpoints list`.",
        exit_code=CLI_EXIT_USAGE,
    )


def _load_checkpoint_record(store, checkpoint_id):
    try:
        return store.load_checkpoint_record(checkpoint_id)
    except FileNotFoundError as exc:
        raise CliError(
            code="checkpoint_not_found",
            message=f"unknown checkpoint: {checkpoint_id}",
            hint="Run `pico-cli checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc


def _preview_restore(manager, checkpoint_id):
    try:
        return manager.preview_restore(checkpoint_id)
    except FileNotFoundError as exc:
        raise CliError(
            code="checkpoint_not_found",
            message=f"unknown checkpoint: {checkpoint_id}",
            hint="Run `pico-cli checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc


def _apply_restore(manager, checkpoint_id):
    try:
        return manager.apply_restore(checkpoint_id)
    except FileNotFoundError as exc:
        raise CliError(
            code="checkpoint_not_found",
            message=f"unknown checkpoint: {checkpoint_id}",
            hint="Run `pico-cli checkpoints list`.",
            exit_code=CLI_EXIT_USAGE,
        ) from exc


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
