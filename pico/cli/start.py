"""One-shot and interactive CLI startup flows."""

from contextlib import contextmanager
import signal
import shlex
import sys
import threading

from pico.security.redaction import redact_text


_RUNTIME_ERROR_MESSAGE = "agent runtime failed"


def _safe_text(agent, value):
    redactor = getattr(agent, "redact_text", None)
    return (redactor if callable(redactor) else redact_text)(value)


def _interrupt_exit_code(exc):
    signal_number = getattr(exc, "signal_number", None)
    return 128 + signal_number if type(signal_number) is int else 130


def _print_session_tree(agent):
    tree = agent.session_store.load_tree(agent.session["id"])
    active = {entry["id"] for entry in tree.active_path}
    print(f"active leaf: {tree.leaf_id or '-'}")
    for entry in tree.entries:
        marker = "*" if entry["id"] in active else " "
        print(
            f"{marker} {entry['id']} {entry['type']} parent={entry['parent_id'] or '-'}"
        )


def _rewind_options(tokens):
    workspace = False
    confirmed = False
    summary = False
    focus = ""
    for token in tokens:
        if token == "--workspace":
            workspace = True
        elif token == "--yes":
            confirmed = True
        elif token == "--summary":
            summary = True
        elif token.startswith("--summary="):
            summary = True
            focus = token.partition("=")[2]
        else:
            raise ValueError(f"unknown rewind option: {token}")
    return workspace, confirmed, summary, focus


def _print_workspace_rewind_preview(preview):
    counts = preview.get("decision_counts", {})
    rendered = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
    print(f"workspace restore plan: {preview.get('status', 'invalid')}")
    print(f"checkpoint: {preview.get('workspace_checkpoint_id', '-')}")
    print(f"entries: {rendered or 'none'}")
    for entry in preview.get("entries", []):
        print(
            f"- {entry.get('decision', 'unknown')}: "
            f"{entry.get('path', '-') or '-'} "
            f"({entry.get('reason', '-') or '-'})"
        )


def _default_confirm(message):
    try:
        answer = input(message)
    except EOFError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


def _handle_repl_session_command(agent, user_input, *, confirm=_default_confirm):
    try:
        tokens = shlex.split(user_input)
    except ValueError as exc:
        print(f"error: {_safe_text(agent, exc)}")
        return True
    command = tokens[0] if tokens else ""
    try:
        if command == "/tree" and len(tokens) == 1:
            _print_session_tree(agent)
            return True
        if command == "/compact":
            result = agent.compact_session(
                focus=" ".join(tokens[1:]),
                reason="manual_repl",
            )
            print(
                "compacted: "
                f"{result.tokens_before} -> {result.tokens_after} tokens "
                f"({result.compression_ratio:.2%})"
            )
            return True
        if command == "/fork" and len(tokens) == 2:
            entry = agent.fork_session(tokens[1])
            print(f"forked at {entry['parent_id']}; leaf={entry['id']}")
            return True
        if command == "/checkpoint":
            checkpoint = agent.create_manual_checkpoint(" ".join(tokens[1:]))
            print(f"checkpoint: {checkpoint['checkpoint_id']}")
            return True
        if command == "/rewind" and len(tokens) >= 2:
            workspace, confirmed, summary, focus = _rewind_options(tokens[2:])
            if workspace and not confirmed:
                preview = agent.preview_workspace_rewind(tokens[1])
                _print_workspace_rewind_preview(preview)
                if not confirm("restore workspace and rewind session? [y/N] "):
                    print("workspace rewind cancelled")
                    return True
                confirmed = True
            result = agent.rewind_session(
                tokens[1],
                summary=summary,
                focus=focus,
                workspace=workspace,
                confirmed=confirmed,
            )
            entry = result.rewind_entry if summary or workspace else result
            print(f"rewound to {entry['parent_id']}; leaf={entry['id']}")
            return True
        if command == "/clone" and len(tokens) >= 3 and tokens[1] == "--to-worktree":
            result = agent.session_store.clone_to_worktree(
                agent.session["id"],
                tokens[2],
            )
            print(
                f"cloned session {result['session_id']} to {result['workspace_root']}"
            )
            return True
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {_safe_text(agent, exc)}")
        return True
    return command in {
        "/tree",
        "/compact",
        "/checkpoint",
        "/fork",
        "/rewind",
        "/clone",
    }


@contextmanager
def _cli_interrupt_boundary():
    if (
        not hasattr(signal, "SIGTERM")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def interrupt(signum, _frame):
        error = KeyboardInterrupt("terminated")
        error.signal_number = signum
        raise error

    signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_agent_once(agent, prompt_tokens):
    prompt = " ".join(prompt_tokens).strip()
    if not prompt:
        return 0
    print()
    with _cli_interrupt_boundary():
        finalize_started = False
        try:
            answer = agent.ask(prompt)
            finalize = getattr(agent, "finalize_sandbox_session", None)
            finalize_started = callable(finalize)
            sandbox = finalize() if finalize_started else None
            print(_safe_text(agent, answer))
            if sandbox is not None and sandbox["status"] != "no_changes_discarded":
                counts = sandbox["artifact"]["counts"]
                candidates = counts.get("candidate", 0) + counts.get(
                    "high_risk_candidate", 0
                )
                high_risk = counts.get("high_risk_candidate", 0)
                blocked = sum(
                    counts.get(name, 0)
                    for name in (
                        "blocked_sensitive",
                        "blocked_size",
                        "blocked_type",
                    )
                )
                print(
                    _safe_text(
                        agent,
                        "\n".join(
                            (
                                f"Sandbox session: {sandbox['sandbox_id']}",
                                f"State: {sandbox['session_state']}",
                                "Changes: "
                                f"{candidates} candidate, {high_risk} high-risk, "
                                f"{blocked} blocked, "
                                f"{sandbox.get('generated_count', 0)} generated (ignored)",
                                f"Review: pico sandbox diff {sandbox['sandbox_id']}",
                                f"Apply: pico sandbox apply {sandbox['sandbox_id']}",
                                f"Discard: pico sandbox discard {sandbox['sandbox_id']}",
                            )
                        ),
                    )
                )
        except KeyboardInterrupt as exc:
            if not finalize_started:
                try:
                    finalize = getattr(agent, "finalize_sandbox_session", None)
                    if callable(finalize):
                        finalize()
                except Exception:  # noqa: BLE001 - preserve interrupt as primary
                    print(_RUNTIME_ERROR_MESSAGE, file=sys.stderr)
            return _interrupt_exit_code(exc)
        except Exception:  # noqa: BLE001 - the CLI is the ordinary-exception boundary
            if not finalize_started:
                try:
                    finalize = getattr(agent, "finalize_sandbox_session", None)
                    if callable(finalize):
                        finalize()
                except Exception:
                    pass
            print(_RUNTIME_ERROR_MESSAGE, file=sys.stderr)
            return 1
    return 0


def _process_repl_input(
    agent,
    user_input,
    *,
    confirm=_default_confirm,
    render_answer=print,
    render_error=None,
):
    if not user_input:
        return None
    if user_input in {"/exit", "/quit"}:
        return 0
    if user_input == "/help":
        from .help import HELP_DETAILS

        print(HELP_DETAILS)
        return None
    if user_input == "/memory":
        task_summary = agent.memory.task_summary
        recent_files = agent.memory.recent_files
        print(f"task: {task_summary or '(empty)'}")
        print(f"recent: {', '.join(recent_files) if recent_files else '(empty)'}")
        try:
            entries = agent.memory_store.list()
        except Exception:  # noqa: BLE001 - listing failure must not end the REPL
            entries = []
        if entries:
            print("\nMemory files:")
            for entry in entries:
                print(f"- {entry.path} ({entry.size_chars} chars)")
        else:
            print(
                "\nMemory files: (none — use /remember <text> or edit "
                ".pico/memory/notes/*.md)"
            )
        return None
    if user_input == "/session":
        print(agent.session_path)
        return None
    if _handle_repl_session_command(agent, user_input, confirm=confirm):
        return None
    if user_input == "/reset":
        agent.reset()
        print("session reset")
        return None
    if user_input == "/remember" or user_input.startswith("/remember "):
        note = user_input[len("/remember") :].strip()
        if not note:
            print("usage: /remember <text>")
            return None
        if agent.token_accounting.count_text(note) > 1_024:
            print("error: note exceeds 1024 model tokens")
            return None
        try:
            total = agent.memory_store.append_agent_note(scope="workspace", note=note)
        except ValueError as exc:
            print(f"error: {_safe_text(agent, exc)}")
            return None
        print(f"saved (chars_total={total})")
        return None
    if user_input == "/memory-review":
        try:
            content = agent.memory_store.read("workspace/agent_notes.md")
        except FileNotFoundError:
            print("(no agent_notes.md yet)")
        except (OSError, RuntimeError, ValueError):
            print("error: agent notes could not be read safely")
        else:
            print(f"agent_notes.md ({len(content)} chars):\n\n{content}")
            print("To edit: vim .pico/memory/agent_notes.md")
        return None
    if user_input.startswith("/"):
        command = user_input.split(maxsplit=1)[0]
        message = f"unknown command: {command}; type /help for available commands"
        if render_error is None:
            print(message)
        else:
            render_error(message)
        return None

    print()
    try:
        render_answer(_safe_text(agent, agent.ask(user_input)))
    except Exception:  # noqa: BLE001 - preserve BaseException interrupt semantics
        if render_error is None:
            print(_RUNTIME_ERROR_MESSAGE, file=sys.stderr)
        else:
            render_error(_RUNTIME_ERROR_MESSAGE)
        return 1
    return None


def _finish_repl(agent, code):
    try:
        finalize = getattr(agent, "finalize_sandbox_session", None)
        if callable(finalize):
            finalize()
    except Exception:  # noqa: BLE001 - the CLI is the ordinary-exception boundary
        print(_RUNTIME_ERROR_MESSAGE, file=sys.stderr)
        return 1
    return code


def run_repl(
    agent,
    *,
    welcome="",
    model="",
    plain=False,
    no_color=False,
    show_header=True,
):
    with _cli_interrupt_boundary():
        try:
            if not plain:
                from pico.tui.app import run_tui, should_use_tui

                if should_use_tui():
                    return _finish_repl(
                        agent,
                        run_tui(
                            agent,
                            model=model,
                            no_color=no_color,
                            handle_input=_process_repl_input,
                            show_header=show_header,
                        ),
                    )
            if welcome:
                print(welcome)
            while True:
                try:
                    user_input = input("\npico> ").strip()
                except EOFError:
                    print("")
                    return _finish_repl(agent, 0)

                result = _process_repl_input(agent, user_input)
                if result is not None:
                    return _finish_repl(agent, result)
        except KeyboardInterrupt as exc:
            print("")
            return _finish_repl(agent, _interrupt_exit_code(exc))
