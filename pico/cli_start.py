"""One-shot and interactive CLI startup flows."""

import sys
from pathlib import Path

from .security import redact_text


def _safe_text(agent, value):
    redactor = getattr(agent, "redact_text", None)
    return (redactor if callable(redactor) else redact_text)(value)


def run_agent_once(agent, prompt_tokens):
    prompt = " ".join(prompt_tokens).strip()
    if not prompt:
        return 0
    print()
    try:
        print(_safe_text(agent, agent.ask(prompt)))
    except RuntimeError as exc:
        print(_safe_text(agent, str(exc))[:300], file=sys.stderr)
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
            print(_safe_text(agent, agent.ask(user_input)))
        except RuntimeError as exc:
            print(_safe_text(agent, str(exc))[:300], file=sys.stderr)
