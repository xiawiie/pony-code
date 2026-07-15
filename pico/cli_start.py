"""One-shot and interactive CLI startup flows."""

from contextlib import contextmanager
import signal
import sys
import threading

from .security import redact_text


_RUNTIME_ERROR_MESSAGE = "agent runtime failed"


def _safe_text(agent, value):
    redactor = getattr(agent, "redact_text", None)
    return (redactor if callable(redactor) else redact_text)(value)


def _interrupt_exit_code(exc):
    signal_number = getattr(exc, "signal_number", None)
    return 128 + signal_number if type(signal_number) is int else 130


@contextmanager
def _cli_interrupt_boundary():
    if not hasattr(signal, "SIGTERM") or threading.current_thread() is not threading.main_thread():
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
                        )
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


def run_repl(agent):
    def finish(code):
        try:
            finalize = getattr(agent, "finalize_sandbox_session", None)
            if callable(finalize):
                finalize()
        except Exception:  # noqa: BLE001 - the CLI is the ordinary-exception boundary
            print(_RUNTIME_ERROR_MESSAGE, file=sys.stderr)
            return 1
        return code

    with _cli_interrupt_boundary():
        try:
            while True:
                try:
                    user_input = input("\npico> ").strip()
                except EOFError:
                    print("")
                    return finish(0)

                if not user_input:
                    continue
                if user_input in {"/exit", "/quit"}:
                    return finish(0)
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
                    try:
                        content = agent.memory_store.read(
                            "workspace/agent_notes.md"
                        )
                    except FileNotFoundError:
                        print("(no agent_notes.md yet)")
                    except (OSError, RuntimeError, ValueError):
                        print("error: agent notes could not be read safely")
                    else:
                        print(f"agent_notes.md ({len(content)} chars):\n\n{content}")
                        print("To edit: vim .pico/memory/agent_notes.md")
                    continue

                print()
                try:
                    print(_safe_text(agent, agent.ask(user_input)))
                except Exception:  # noqa: BLE001 - preserve BaseException semantics
                    print(_RUNTIME_ERROR_MESSAGE, file=sys.stderr)
                    return finish(1)
        except KeyboardInterrupt as exc:
            print("")
            return finish(_interrupt_exit_code(exc))
