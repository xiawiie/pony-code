# Pico CLI Surface Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Pico's hidden prompt-token CLI dispatch with an explicit, discoverable, scriptable CLI Surface while preserving existing one-shot, REPL, checkpoint, and run behavior.

**Architecture:** Keep `pico.cli` as the public console entry shim and compatibility export module, then move parsing, command dispatch, output rendering, typed errors, and diagnostics into focused CLI modules. The CLI remains a presenter and dispatcher over existing runtime boundaries: `Pico`, `SessionStore`, `RunStore`, `CheckpointStore`, and `RecoveryManager`.

**Tech Stack:** Python 3.10+, stdlib `argparse`, stdlib `json`, stdlib `urllib`, pytest, existing Pico runtime modules, no third-party CLI framework.

---

## File Structure

- Create `pico/cli_errors.py`: typed CLI errors, exit codes, suggestion helper.
- Create `pico/cli_output.py`: JSON envelopes, text rendering helpers, color/TTY decisions, quiet handling.
- Create `pico/cli_parser.py`: explicit command parsing while preserving legacy parser compatibility, without importing `pico.cli`.
- Create `pico/cli_commands.py`: command handlers for `run`, `repl`, `runs`, `sessions`, and `checkpoints`.
- Create `pico/cli_diagnostics.py`: `status`, `config show`, and `doctor` data collection.
- Modify `pico/cli.py`: keep public exports and delegate command routing to the new modules.
- Modify `README.md`: document explicit command usage after behavior is implemented and verified.
- Add `tests/test_cli_output.py`: output envelopes, color, quiet behavior.
- Add `tests/test_cli_parser.py`: explicit command parsing and compatibility parsing.
- Add `tests/test_cli_commands.py`: `run`, `repl`, compatibility, help, suggestions.
- Add `tests/test_cli_diagnostics.py`: status, config show, doctor, offline doctor.
- Extend `tests/test_recovery_cli.py`: JSON envelope and no-mutation coverage for recovery commands.
- Keep `tests/test_public_api_contract.py` passing without changing public import names.

## Task 1: Lock Current CLI Compatibility

**Files:**
- Modify: `tests/test_recovery_cli.py`
- Test: `tests/test_recovery_cli.py`

- [ ] **Step 1: Add characterization tests for legacy prompt and no-argument REPL**

Append these tests to `tests/test_recovery_cli.py`:

```python
def test_legacy_prompt_still_runs_one_shot(tmp_path, monkeypatch, capsys):
    called = {}

    def fake_build_agent(args):
        called["cwd"] = args.cwd
        called["prompt"] = list(args.prompt)

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            approval_policy = "auto"
            session = {"id": "s"}

            def ask(self, message):
                called["asked"] = message
                return "answer"

        return FakeAgent()

    monkeypatch.setattr("pico.cli.build_agent", fake_build_agent)
    monkeypatch.setattr("pico.cli.build_welcome", lambda agent, model, host: "")

    code = main(["--cwd", str(tmp_path), "inspect", "tests"])

    assert code == 0
    assert called["asked"] == "inspect tests"
    assert "answer" in capsys.readouterr().out


def test_no_argument_cli_enters_repl_and_exits_on_eof(tmp_path, monkeypatch):
    called = {}

    def fake_build_agent(args):
        called["built"] = True

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            approval_policy = "auto"
            session = {"id": "s"}

            def memory_text(self):
                return ""

            def reset(self):
                called["reset"] = True

        return FakeAgent()

    def fake_input(prompt):
        raise EOFError

    monkeypatch.setattr("pico.cli.build_agent", fake_build_agent)
    monkeypatch.setattr("pico.cli.build_welcome", lambda agent, model, host: "")
    monkeypatch.setattr("builtins.input", fake_input)

    code = main(["--cwd", str(tmp_path)])

    assert code == 0
    assert called["built"] is True
```

- [ ] **Step 2: Run compatibility tests**

Run:

```bash
uv run pytest tests/test_recovery_cli.py tests/test_public_api_contract.py -q
```

Expected: all tests pass before any CLI refactor.

- [ ] **Step 3: Commit compatibility tests**

```bash
git add tests/test_recovery_cli.py
git commit -m "test: characterize legacy cli behavior"
```

## Task 2: Add CLI Error And Output Primitives

**Files:**
- Create: `pico/cli_errors.py`
- Create: `pico/cli_output.py`
- Create: `tests/test_cli_output.py`

- [ ] **Step 1: Write failing tests for envelopes, errors, quiet, and color**

Create `tests/test_cli_output.py`:

```python
import json

from pico.cli_errors import CLI_EXIT_USAGE, CliError, suggest
from pico.cli_output import (
    error_envelope,
    format_json,
    should_use_color,
    success_envelope,
)


def test_success_envelope_has_stable_shape():
    payload = success_envelope("runs_list", [{"run_id": "run_1"}])

    assert payload == {
        "ok": True,
        "kind": "runs_list",
        "data": [{"run_id": "run_1"}],
    }


def test_error_envelope_redacts_to_error_shape():
    error = CliError(
        code="checkpoint_not_found",
        message="Unknown checkpoint: ckpt_missing",
        hint="Run `pico checkpoints list`.",
        exit_code=CLI_EXIT_USAGE,
    )

    payload = error_envelope(error)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "checkpoint_not_found"
    assert payload["error"]["message"] == "Unknown checkpoint: ckpt_missing"
    assert payload["error"]["hint"] == "Run `pico checkpoints list`."


def test_format_json_outputs_parseable_json_with_newline():
    text = format_json(success_envelope("status", {"ok": True}))

    assert text.endswith("\n")
    assert json.loads(text) == {"ok": True, "kind": "status", "data": {"ok": True}}


def test_should_use_color_respects_cli_and_environment():
    class Tty:
        def isatty(self):
            return True

    assert should_use_color(stream=Tty(), environ={}, no_color=False) is True
    assert should_use_color(stream=Tty(), environ={"NO_COLOR": "1"}, no_color=False) is False
    assert should_use_color(stream=Tty(), environ={"TERM": "dumb"}, no_color=False) is False
    assert should_use_color(stream=Tty(), environ={}, no_color=True) is False


def test_suggest_returns_close_match():
    assert suggest("chekpoints", ["checkpoints", "runs"]) == "checkpoints"
    assert suggest("zzzz", ["checkpoints", "runs"]) == ""
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_cli_output.py -q
```

Expected: import failure for `pico.cli_errors` and `pico.cli_output`.

- [ ] **Step 3: Add `pico/cli_errors.py`**

Create `pico/cli_errors.py`:

```python
"""Typed CLI errors and exit-code mapping."""

from difflib import get_close_matches


CLI_EXIT_SUCCESS = 0
CLI_EXIT_RUNTIME = 1
CLI_EXIT_USAGE = 2
CLI_EXIT_CONFIG = 3
CLI_EXIT_APPROVAL = 4
CLI_EXIT_INTERNAL = 5


class CliError(Exception):
    def __init__(self, code, message, hint="", exit_code=CLI_EXIT_USAGE, details=None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.hint = str(hint or "")
        self.exit_code = int(exit_code)
        self.details = dict(details or {})


def suggest(value, choices):
    matches = get_close_matches(str(value), [str(choice) for choice in choices], n=1, cutoff=0.6)
    return matches[0] if matches else ""
```

- [ ] **Step 4: Add `pico/cli_output.py`**

Create `pico/cli_output.py`:

```python
"""CLI output helpers for human and machine output."""

import json
import os
import sys


def success_envelope(kind, data):
    return {
        "ok": True,
        "kind": str(kind),
        "data": data,
    }


def error_envelope(error):
    payload = {
        "ok": False,
        "error": {
            "code": error.code,
            "message": error.message,
        },
    }
    if error.hint:
        payload["error"]["hint"] = error.hint
    if error.details:
        payload["error"]["details"] = error.details
    return payload


def format_json(payload):
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def should_use_color(stream=None, environ=None, no_color=False):
    stream = stream or sys.stdout
    environ = os.environ if environ is None else environ
    if no_color:
        return False
    if environ.get("NO_COLOR") is not None:
        return False
    if environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())
```

- [ ] **Step 5: Run tests to verify pass**

Run:

```bash
uv run pytest tests/test_cli_output.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add pico/cli_errors.py pico/cli_output.py tests/test_cli_output.py
git commit -m "feat: add cli output primitives"
```

## Task 3: Add Explicit Parser Model

**Files:**
- Create: `pico/cli_parser.py`
- Create: `tests/test_cli_parser.py`
- Modify: `pico/cli.py`

- [ ] **Step 1: Write failing parser tests**

Create `tests/test_cli_parser.py`:

```python
from pico.cli import build_arg_parser
from pico.cli_parser import KNOWN_TOP_LEVEL_COMMANDS, parse_cli_invocation


def test_parse_run_command_with_prompt():
    invocation = parse_cli_invocation(["--cwd", "/repo", "run", "fix", "tests"], build_arg_parser())

    assert invocation.command == "run"
    assert invocation.command_args == ["fix", "tests"]
    assert invocation.runtime_args.cwd == "/repo"
    assert invocation.legacy_prompt is False


def test_parse_repl_command():
    invocation = parse_cli_invocation(["repl"], build_arg_parser())

    assert invocation.command == "repl"
    assert invocation.command_args == []


def test_parse_legacy_prompt_when_head_is_not_command():
    invocation = parse_cli_invocation(["inspect", "tests"], build_arg_parser())

    assert invocation.command == "run"
    assert invocation.command_args == ["inspect", "tests"]
    assert invocation.legacy_prompt is True


def test_reserved_command_names_are_known():
    assert {"run", "repl", "status", "doctor", "config", "runs", "sessions", "checkpoints"}.issubset(
        KNOWN_TOP_LEVEL_COMMANDS
    )
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_cli_parser.py -q
```

Expected: import failure for `pico.cli_parser`.

- [ ] **Step 3: Add `pico/cli_parser.py`**

Create `pico/cli_parser.py` with this initial parser model:

```python
"""CLI parser helpers for explicit and compatibility command dispatch."""

from dataclasses import dataclass


KNOWN_TOP_LEVEL_COMMANDS = {
    "run",
    "repl",
    "status",
    "doctor",
    "config",
    "runs",
    "sessions",
    "checkpoints",
    "help",
}


@dataclass
class CliInvocation:
    command: str
    command_args: list
    runtime_args: object
    legacy_prompt: bool = False


def parse_cli_invocation(argv, parser):
    argv = list(argv or [])
    args, extra = parser.parse_known_args(argv)
    tokens = list(args.prompt)
    if extra:
        tokens.extend(extra)
    if not tokens:
        return CliInvocation("repl", [], args, legacy_prompt=False)
    head = tokens[0]
    if head in KNOWN_TOP_LEVEL_COMMANDS:
        return CliInvocation(head, tokens[1:], args, legacy_prompt=False)
    return CliInvocation("run", tokens, args, legacy_prompt=True)
```

- [ ] **Step 4: Run parser tests**

Run:

```bash
uv run pytest tests/test_cli_parser.py -q
```

Expected: all parser tests pass.

- [ ] **Step 5: Run public API tests**

Run:

```bash
uv run pytest tests/test_public_api_contract.py -q
```

Expected: public imports still pass.

- [ ] **Step 6: Commit**

```bash
git add pico/cli_parser.py tests/test_cli_parser.py
git commit -m "feat: add explicit cli invocation parser"
```

## Task 4: Add Explicit `run` And `repl` Dispatch

**Files:**
- Create: `pico/cli_commands.py`
- Modify: `pico/cli.py`
- Create: `tests/test_cli_commands.py`

- [ ] **Step 1: Write failing command tests**

Create `tests/test_cli_commands.py`:

```python
from pico.cli import main


def _install_fake_agent(monkeypatch, tmp_path, called):
    def fake_build_agent(args):
        called["built"] = True
        called["prompt"] = list(getattr(args, "prompt", []))

        class FakeAgent:
            model_client = type("MC", (), {"model": "x"})()
            workspace = type("W", (), {"cwd": str(tmp_path), "branch": "main"})()
            approval_policy = "auto"
            session = {"id": "s"}
            session_path = str(tmp_path / ".pico" / "sessions" / "s.json")

            def ask(self, message):
                called["asked"] = message
                return "answer"

            def memory_text(self):
                return "memory"

            def reset(self):
                called["reset"] = True

        return FakeAgent()

    monkeypatch.setattr("pico.cli.build_agent", fake_build_agent)
    monkeypatch.setattr("pico.cli.build_welcome", lambda agent, model, host: "")


def test_run_command_calls_agent_once(tmp_path, monkeypatch, capsys):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    code = main(["--cwd", str(tmp_path), "run", "fix", "tests"])

    assert code == 0
    assert called["asked"] == "fix tests"
    assert "answer" in capsys.readouterr().out


def test_repl_command_exits_on_eof(tmp_path, monkeypatch):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError()))

    code = main(["--cwd", str(tmp_path), "repl"])

    assert code == 0
    assert called["built"] is True


def test_legacy_prompt_remains_silent_compatibility(tmp_path, monkeypatch, capsys):
    called = {}
    _install_fake_agent(monkeypatch, tmp_path, called)

    code = main(["--cwd", str(tmp_path), "fix", "tests"])

    assert code == 0
    out = capsys.readouterr().out
    assert "answer" in out
    assert "pico run" not in out
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_cli_commands.py -q
```

Expected: `pico run ...` is treated as legacy prompt and fails the expected `called["asked"]` assertion.

- [ ] **Step 3: Add `pico/cli_commands.py` startup helpers**

Create `pico/cli_commands.py`:

```python
"""Command handlers for Pico's explicit CLI Surface."""

import sys


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
            from .cli import HELP_DETAILS

            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            print(agent.memory_text())
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue

        print()
        try:
            print(agent.ask(user_input))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
```

- [ ] **Step 4: Modify `pico/cli.py` main dispatch**

In `pico/cli.py`, import parser and command helpers near the existing imports:

```python
from .cli_commands import run_agent_once, run_repl
from .cli_parser import parse_cli_invocation
```

Near the start of `main()`, parse the explicit invocation once and reuse its runtime args:

```python
    parser = build_arg_parser()
    invocation = parse_cli_invocation(argv, parser)
    args = invocation.runtime_args
```

Keep the existing recovery command short-circuit until Task 5 moves recovery routing behind the explicit dispatcher. After the welcome print, replace the legacy one-shot and REPL block with:

```python
    if invocation.command == "run":
        return run_agent_once(agent, invocation.command_args)
    if invocation.command == "repl":
        return run_repl(agent)
```

Remove the duplicate `parser.parse_known_args(argv)` call once `parse_cli_invocation(argv, parser)` owns argument parsing.

- [ ] **Step 5: Run focused command tests**

Run:

```bash
uv run pytest tests/test_cli_commands.py tests/test_recovery_cli.py tests/test_public_api_contract.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add pico/cli.py pico/cli_commands.py tests/test_cli_commands.py
git commit -m "feat: add explicit run and repl commands"
```

## Task 5: Protocolize Runs And Checkpoints With JSON Output

**Files:**
- Modify: `pico/cli_commands.py`
- Modify: `pico/cli.py`
- Modify: `tests/test_recovery_cli.py`

- [ ] **Step 1: Add failing tests for JSON envelopes and preview safety**

Append to `tests/test_recovery_cli.py`:

```python
import json


def test_checkpoints_list_json_uses_success_envelope(tmp_path, capsys):
    store = CheckpointStore(tmp_path)
    store.write_checkpoint_record(new_checkpoint_record("ckpt_1", "turn", "s", "r", "t", "", str(tmp_path)))

    code = main(["--cwd", str(tmp_path), "--format", "json", "checkpoints", "list"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["kind"] == "checkpoints_list"
    assert payload["data"][0]["checkpoint_id"] == "ckpt_1"


def test_runs_list_json_uses_success_envelope(tmp_path, capsys):
    run_dir = tmp_path / ".pico" / "runs" / "run_1"
    run_dir.mkdir(parents=True)

    code = main(["--cwd", str(tmp_path), "--format", "json", "runs", "list"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "kind": "runs_list", "data": [{"run_id": "run_1"}]}
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_recovery_cli.py::test_checkpoints_list_json_uses_success_envelope tests/test_recovery_cli.py::test_runs_list_json_uses_success_envelope -q
```

Expected: parser rejects `--format` or output is not a JSON envelope.

- [ ] **Step 3: Add output flags to `build_arg_parser()`**

In `pico/cli.py`, add these arguments to `build_arg_parser()`:

```python
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format for inspection commands.")
    parser.add_argument("--quiet", action="store_true", help="Suppress non-essential human output.")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output.")
    parser.add_argument("--no-input", action="store_true", help="Disable interactive prompts.")
```

- [ ] **Step 4: Return data from checkpoint and run command helpers**

In `pico/cli_commands.py`, add:

```python
from pathlib import Path

from .checkpoint_store import CheckpointStore
from .cli_errors import CLI_EXIT_USAGE, CliError
from .cli_output import format_json, success_envelope
from .recovery_checkpoint_writer import RecoveryCheckpointWriter
from .recovery_manager import RecoveryManager


def print_result(kind, data, args, text_renderer):
    if getattr(args, "format", "text") == "json":
        print(format_json(success_envelope(kind, data)), end="")
        return 0
    text = text_renderer(data)
    if text:
        print(text)
    return 0


def handle_checkpoints(root, tokens, args):
    store = CheckpointStore(root)
    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    if sub == "list":
        records = store.list_checkpoint_records()
        return print_result(
            "checkpoints_list",
            records,
            args,
            lambda data: "\n".join(f"{record['checkpoint_id']}\t{record['checkpoint_type']}\t{record.get('created_at', '')}" for record in data),
        )
    if sub == "show" and rest:
        try:
            record = store.load_checkpoint_record(rest[0])
        except FileNotFoundError as exc:
            raise CliError("checkpoint_not_found", f"Unknown checkpoint: {rest[0]}", "Run `pico checkpoints list`.") from exc
        return print_result("checkpoints_show", record, args, lambda data: format_json(data).rstrip())
    if sub == "preview-restore" and rest:
        manager = RecoveryManager(store, root, checkpoint_writer=RecoveryCheckpointWriter(store, root))
        plan = manager.preview_restore(rest[0])
        return print_result("checkpoint_restore_preview", plan, args, lambda data: format_json(data).rstrip())
    if sub == "restore" and rest:
        checkpoint_id = rest[0]
        apply_flag = "--apply" in rest[1:]
        manager = RecoveryManager(store, root, checkpoint_writer=RecoveryCheckpointWriter(store, root))
        result = manager.apply_restore(checkpoint_id) if apply_flag else manager.preview_restore(checkpoint_id)
        kind = "checkpoint_restore_applied" if apply_flag else "checkpoint_restore_preview"
        return print_result(kind, result, args, lambda data: format_json(data).rstrip())
    if sub == "prune":
        apply_flag = "--apply" in rest
        result = store.prune(dry_run=not apply_flag)
        return print_result("checkpoints_prune", result, args, lambda data: format_json(data).rstrip())
    raise CliError(
        "invalid_checkpoints_usage",
        "usage: pico checkpoints {list | show <id> | preview-restore <id> | restore <id> [--apply] | prune [--apply]}",
        exit_code=CLI_EXIT_USAGE,
    )


def handle_runs(root, tokens, args):
    runs_root = Path(root) / ".pico" / "runs"
    sub = tokens[0] if tokens else "list"
    rest = tokens[1:]
    if sub == "list":
        rows = []
        if runs_root.exists():
            rows = [{"run_id": entry.name} for entry in sorted(runs_root.iterdir()) if entry.is_dir()]
        return print_result("runs_list", rows, args, lambda data: "\n".join(row["run_id"] for row in data))
    if sub == "show" and rest:
        run_dir = runs_root / rest[0]
        if not run_dir.exists():
            raise CliError("run_not_found", f"Unknown run: {rest[0]}", "Run `pico runs list`.")
        payload = {}
        for name in ("task_state.json", "report.json"):
            path = run_dir / name
            if path.exists():
                payload[name] = path.read_text(encoding="utf-8")
        trace_path = run_dir / "trace.jsonl"
        if trace_path.exists():
            payload["trace.jsonl"] = trace_path.read_text(encoding="utf-8")
        return print_result(
            "runs_show",
            payload,
            args,
            lambda data: "\n".join(f"--- {name} ---\n{content}" for name, content in data.items()),
        )
    raise CliError("invalid_runs_usage", "usage: pico runs {list | show <run_id>}", exit_code=CLI_EXIT_USAGE)
```

- [ ] **Step 5: Route runs and checkpoints through `cli_commands`**

In `pico/cli.py`, import:

```python
from .cli_commands import handle_checkpoints, handle_runs, run_agent_once, run_repl
from .cli_errors import CliError
from .cli_output import error_envelope, format_json
```

In `main()`, call `handle_checkpoints()` and `handle_runs()` for explicit and compatibility recovery commands. Wrap `CliError`:

```python
    try:
        if invocation.command == "checkpoints":
            return handle_checkpoints(root, invocation.command_args, args)
        if invocation.command == "runs":
            return handle_runs(root, invocation.command_args, args)
    except CliError as exc:
        if getattr(args, "format", "text") == "json":
            print(format_json(error_envelope(exc)), end="")
        else:
            print(exc.message, file=sys.stderr)
            if exc.hint:
                print(exc.hint, file=sys.stderr)
        return exc.exit_code
```

- [ ] **Step 6: Run recovery tests**

Run:

```bash
uv run pytest tests/test_recovery_cli.py -q
```

Expected: all recovery CLI tests pass.

- [ ] **Step 7: Commit**

```bash
git add pico/cli.py pico/cli_commands.py tests/test_recovery_cli.py
git commit -m "feat: add structured recovery cli output"
```

## Task 6: Add Sessions, Status, And Config Show

**Files:**
- Modify: `pico/cli_commands.py`
- Create: `pico/cli_diagnostics.py`
- Create: `tests/test_cli_diagnostics.py`

- [ ] **Step 1: Write failing diagnostics tests**

Create `tests/test_cli_diagnostics.py`:

```python
import json

from pico.cli import main


def test_status_json_reports_storage_without_building_agent(tmp_path, monkeypatch, capsys):
    (tmp_path / ".pico" / "sessions").mkdir(parents=True)
    (tmp_path / ".pico" / "runs" / "run_1").mkdir(parents=True)

    def fail_build_agent(args):
        raise AssertionError("status must not build a Pico agent")

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)

    code = main(["--cwd", str(tmp_path), "--format", "json", "status"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "status"
    assert payload["data"]["storage"]["sessions"] is True
    assert "memory" not in payload["data"]


def test_config_show_json_reports_sources_without_secret_values(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("PICO_PROVIDER=deepseek\nPICO_DEEPSEEK_API_KEY=secret-value\n", encoding="utf-8")

    def fail_build_agent(args):
        raise AssertionError("config show must not build a Pico agent")

    monkeypatch.setattr("pico.cli.build_agent", fail_build_agent)

    code = main(["--cwd", str(tmp_path), "--format", "json", "config", "show"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "config_show"
    assert payload["data"]["provider"]["value"] == "deepseek"
    assert payload["data"]["api_key"]["present"] is True
    assert "secret-value" not in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_cli_diagnostics.py -q
```

Expected: `status` and `config` are not implemented.

- [ ] **Step 3: Add `pico/cli_diagnostics.py` local collectors**

Create `pico/cli_diagnostics.py`:

```python
"""Diagnostics for status, config show, and doctor."""

from pathlib import Path

from .config import load_project_env, provider_env
from .workspace import WorkspaceContext


DEFAULT_PROVIDER = "deepseek"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
PROVIDER_CHOICES = ("ollama", "openai", "anthropic", "deepseek")


def latest_stem(directory, suffix="*.json"):
    path = Path(directory)
    if not path.exists():
        return ""
    files = sorted(path.glob(suffix), key=lambda item: item.stat().st_mtime)
    return files[-1].stem if files else ""


def collect_status(cwd):
    workspace = WorkspaceContext.build(cwd)
    root = Path(workspace.repo_root)
    pico_root = root / ".pico"
    checkpoints = pico_root / "checkpoints" / "records"
    runs = pico_root / "runs"
    sessions = pico_root / "sessions"
    return {
        "workspace": {
            "cwd": workspace.cwd,
            "repo_root": workspace.repo_root,
            "branch": workspace.branch,
            "status": workspace.status,
        },
        "provider": {
            "value": provider_env("PICO_PROVIDER", default=DEFAULT_PROVIDER),
        },
        "model": {
            "value": provider_env("PICO_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",), DEFAULT_DEEPSEEK_MODEL),
        },
        "latest": {
            "session_id": latest_stem(sessions),
            "run_id": latest_stem(runs, "*"),
            "checkpoint_id": latest_stem(checkpoints),
        },
        "storage": {
            "sessions": sessions.exists(),
            "runs": runs.exists(),
            "checkpoints": checkpoints.exists(),
        },
    }


def collect_config(cwd, args):
    workspace = WorkspaceContext.build(cwd)
    loaded = load_project_env(workspace.repo_root)
    provider_value = getattr(args, "provider", None) or provider_env("PICO_PROVIDER", default=DEFAULT_PROVIDER)
    provider_source = "cli_flag" if getattr(args, "provider", None) else ("project_env" if "PICO_PROVIDER" in loaded else "default")
    if provider_value not in PROVIDER_CHOICES:
        provider_source = "invalid"
    api_key_name = "PICO_DEEPSEEK_API_KEY"
    api_key_present = bool(provider_env(api_key_name, ("DEEPSEEK_API_KEY",)))
    api_key_source = "project_env" if api_key_name in loaded else ("shell_env" if api_key_present else "missing")
    return {
        "provider": {
            "value": provider_value,
            "source": provider_source,
            "name": "PICO_PROVIDER",
        },
        "model": {
            "value": provider_env("PICO_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",), DEFAULT_DEEPSEEK_MODEL),
            "source": "project_env" if "PICO_DEEPSEEK_MODEL" in loaded else "default",
        },
        "api_key": {
            "present": api_key_present,
            "source": api_key_source,
            "name": api_key_name,
        },
    }
```

- [ ] **Step 4: Add status/config command handlers**

In `pico/cli_commands.py`, import:

```python
from .cli_diagnostics import collect_config, collect_status
```

Add:

```python
def handle_status(cwd, args):
    return print_result(
        "status",
        collect_status(cwd),
        args,
        lambda data: "\n".join(
            [
                f"workspace\t{data['workspace']['repo_root']}",
                f"branch\t{data['workspace']['branch']}",
                f"provider\t{data['provider']['value']}",
                f"latest_session\t{data['latest']['session_id']}",
                f"latest_run\t{data['latest']['run_id']}",
                f"latest_checkpoint\t{data['latest']['checkpoint_id']}",
            ]
        ),
    )


def handle_config(tokens, cwd, args):
    sub = tokens[0] if tokens else "show"
    if sub != "show":
        raise CliError("invalid_config_usage", "usage: pico config show")
    return print_result("config_show", collect_config(cwd, args), args, lambda data: format_json(data).rstrip())
```

- [ ] **Step 5: Route status/config before building agent**

In `pico/cli.py`, route `status` and `config` before `build_agent(args)`:

```python
    if invocation.command == "status":
        return handle_status(args.cwd, args)
    if invocation.command == "config":
        return handle_config(invocation.command_args, args.cwd, args)
```

- [ ] **Step 6: Run diagnostics tests**

Run:

```bash
uv run pytest tests/test_cli_diagnostics.py -q
```

Expected: all diagnostics tests pass.

- [ ] **Step 7: Commit**

```bash
git add pico/cli.py pico/cli_commands.py pico/cli_diagnostics.py tests/test_cli_diagnostics.py
git commit -m "feat: add cli status and config show"
```

## Task 7: Add Doctor And Offline Diagnostics

**Files:**
- Modify: `pico/cli_diagnostics.py`
- Modify: `pico/cli_commands.py`
- Modify: `tests/test_cli_diagnostics.py`

- [ ] **Step 1: Add failing doctor tests**

Append to `tests/test_cli_diagnostics.py`:

```python
def test_doctor_offline_skips_connectivity(tmp_path, monkeypatch, capsys):
    called = {}

    def fake_connectivity(config):
        called["connectivity"] = True
        return {"status": "ok"}

    monkeypatch.setattr("pico.cli_diagnostics.check_provider_connectivity", fake_connectivity)

    code = main(["--cwd", str(tmp_path), "--format", "json", "doctor", "--offline"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "doctor"
    assert called == {}
    assert payload["data"]["provider_connectivity"]["status"] == "skipped"


def test_doctor_reports_connectivity_as_diagnostic_result(tmp_path, monkeypatch, capsys):
    def fake_connectivity(config):
        return {
            "status": "error",
            "category": "provider_connectivity",
            "message": "connection timed out",
        }

    monkeypatch.setattr("pico.cli_diagnostics.check_provider_connectivity", fake_connectivity)

    code = main(["--cwd", str(tmp_path), "--format", "json", "doctor"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["provider_connectivity"]["category"] == "provider_connectivity"
    assert payload["data"]["provider_connectivity"]["message"] == "connection timed out"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_cli_diagnostics.py::test_doctor_offline_skips_connectivity tests/test_cli_diagnostics.py::test_doctor_reports_connectivity_as_diagnostic_result -q
```

Expected: doctor is not implemented.

- [ ] **Step 3: Add doctor collectors**

In `pico/cli_diagnostics.py`, add:

```python
from urllib import request


def check_provider_connectivity(config, timeout=2):
    provider = config["provider"]["value"]
    if provider == "deepseek":
        url = provider_env("PICO_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",), "https://api.deepseek.com/anthropic")
    elif provider == "openai":
        url = provider_env("PICO_OPENAI_API_BASE", ("OPENAI_API_BASE",), "https://www.right.codes/codex/v1")
    elif provider == "anthropic":
        url = provider_env("PICO_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",), "https://www.right.codes/claude/v1")
    else:
        url = provider_env("PICO_OLLAMA_HOST", default="http://127.0.0.1:11434")
    try:
        with request.urlopen(url, timeout=timeout) as response:
            return {
                "status": "ok",
                "category": "provider_connectivity",
                "url": url,
                "http_status": getattr(response, "status", 0),
            }
    except Exception as exc:
        return {
            "status": "error",
            "category": "provider_connectivity",
            "url": url,
            "message": str(exc),
        }


def collect_doctor(cwd, args, offline=False):
    status = collect_status(cwd)
    config = collect_config(cwd, args)
    root = Path(status["workspace"]["repo_root"])
    storage = {
        "sessions": {"status": "ok" if (root / ".pico" / "sessions").exists() else "missing"},
        "runs": {"status": "ok" if (root / ".pico" / "runs").exists() else "missing"},
        "checkpoints": {"status": "ok" if (root / ".pico" / "checkpoints").exists() else "missing"},
    }
    connectivity = {"status": "skipped", "category": "provider_connectivity"} if offline else check_provider_connectivity(config)
    return {
        "workspace": {"status": "ok", "repo_root": status["workspace"]["repo_root"]},
        "config": {"status": "ok", "provider": config["provider"]},
        "credentials": {"status": "ok" if config["api_key"]["present"] else "missing", "api_key": config["api_key"]},
        "provider_connectivity": connectivity,
        "storage": storage,
        "recovery_store": {"status": "ok" if storage["checkpoints"]["status"] == "ok" else "missing"},
    }
```

- [ ] **Step 4: Add doctor command handler**

In `pico/cli_commands.py`, import:

```python
from .cli_diagnostics import collect_config, collect_doctor, collect_status
```

Add:

```python
def handle_doctor(tokens, cwd, args):
    offline = "--offline" in tokens
    return print_result("doctor", collect_doctor(cwd, args, offline=offline), args, lambda data: format_json(data).rstrip())
```

- [ ] **Step 5: Route doctor before building agent**

In `pico/cli.py`, add before `build_agent(args)`:

```python
    if invocation.command == "doctor":
        return handle_doctor(invocation.command_args, args.cwd, args)
```

- [ ] **Step 6: Run doctor tests**

Run:

```bash
uv run pytest tests/test_cli_diagnostics.py -q
```

Expected: all diagnostics tests pass.

- [ ] **Step 7: Commit**

```bash
git add pico/cli.py pico/cli_commands.py pico/cli_diagnostics.py tests/test_cli_diagnostics.py
git commit -m "feat: add cli doctor diagnostics"
```

## Task 8: Add Help, Suggestions, Quiet, And Color Contracts

**Files:**
- Modify: `pico/cli.py`
- Modify: `pico/cli_parser.py`
- Modify: `pico/cli_commands.py`
- Modify: `pico/cli_output.py`
- Modify: `tests/test_cli_commands.py`
- Modify: `tests/test_cli_output.py`

- [ ] **Step 1: Add failing tests for help and suggestions**

Append to `tests/test_cli_commands.py`:

```python
def test_help_command_shows_examples(capsys):
    code = main(["help"])

    assert code == 0
    out = capsys.readouterr().out
    assert 'pico run "inspect the failing tests"' in out
    assert "Diagnostics:" in out
    assert "providers list" not in out


def test_unknown_command_suggests_close_match(capsys):
    code = main(["chekpoints", "list"])

    assert code == 2
    err = capsys.readouterr().err
    assert "Unknown command: chekpoints" in err
    assert "Did you mean `checkpoints`?" in err
```

- [ ] **Step 2: Add failing test for JSON stdout cleanliness**

Append to `tests/test_recovery_cli.py`:

```python
def test_json_output_contains_no_human_tip_text(tmp_path, capsys):
    code = main(["--cwd", str(tmp_path), "--format", "json", "runs", "list"])

    assert code == 0
    out = capsys.readouterr().out
    assert out.strip().startswith("{")
    assert "Tip:" not in out
    json.loads(out)
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_cli_commands.py::test_help_command_shows_examples tests/test_cli_commands.py::test_unknown_command_suggests_close_match tests/test_recovery_cli.py::test_json_output_contains_no_human_tip_text -q
```

Expected: help and unknown command handling are not implemented.

- [ ] **Step 4: Add root help text**

In `pico/cli_commands.py`, add:

```python
ROOT_HELP = """Pico is a local coding-agent harness for repository-grounded engineering work.

Examples:
  pico run "inspect the failing tests"
  pico repl
  pico status
  pico checkpoints preview-restore <checkpoint-id>

Start:
  run [prompt...]          Run one prompt and exit
  repl                     Start the interactive REPL

Diagnostics:
  status                   Show local harness state
  doctor [--offline]       Run readiness diagnostics
  config show              Show effective configuration and sources

Recovery inspection:
  runs list|show           Inspect run artifacts
  sessions list|show       Inspect saved sessions
  checkpoints ...          Inspect and restore checkpoints

Compatibility:
  pico                     Start REPL
  pico "prompt"            Run a one-shot prompt
"""


def handle_help(tokens):
    print(ROOT_HELP.rstrip())
    return 0
```

- [ ] **Step 5: Add unknown command handling**

In `pico/cli.py`, after parsing invocation and before building the agent:

```python
    if invocation.command == "help":
        return handle_help(invocation.command_args)
    if invocation.command not in {"run", "repl", "status", "doctor", "config", "runs", "sessions", "checkpoints"}:
        suggestion = suggest(invocation.command, sorted(KNOWN_TOP_LEVEL_COMMANDS))
        print(f"Unknown command: {invocation.command}", file=sys.stderr)
        if suggestion:
            print(f"Did you mean `{suggestion}`?", file=sys.stderr)
        return 2
```

Import the required names:

```python
from .cli_commands import handle_checkpoints, handle_config, handle_doctor, handle_help, handle_runs, handle_status, run_agent_once, run_repl
from .cli_errors import CliError, suggest
from .cli_parser import KNOWN_TOP_LEVEL_COMMANDS, parse_cli_invocation
```

- [ ] **Step 6: Preserve JSON stdout cleanliness**

In `pico/cli_commands.py`, ensure `print_result()` prints only `format_json(...)` for JSON:

```python
def print_result(kind, data, args, text_renderer):
    if getattr(args, "format", "text") == "json":
        print(format_json(success_envelope(kind, data)), end="")
        return 0
    text = text_renderer(data)
    if text and not getattr(args, "quiet", False):
        print(text)
    return 0
```

- [ ] **Step 7: Run focused help/output tests**

Run:

```bash
uv run pytest tests/test_cli_commands.py tests/test_cli_output.py tests/test_recovery_cli.py -q
```

Expected: all focused CLI tests pass.

- [ ] **Step 8: Commit**

```bash
git add pico/cli.py pico/cli_parser.py pico/cli_commands.py pico/cli_output.py tests/test_cli_commands.py tests/test_cli_output.py tests/test_recovery_cli.py
git commit -m "feat: polish cli help and output behavior"
```

## Task 9: Document The New CLI Surface

**Files:**
- Modify: `README.md`
- Test: `README.md`

- [ ] **Step 1: Update quick-start examples**

In `README.md`, replace the current one-shot example:

```bash
uv run pico "inspect the test failures and propose a fix"
```

with:

```bash
uv run pico run "inspect the test failures and propose a fix"
```

Add this compatibility note after the one-shot example:

```markdown
The legacy form `uv run pico "prompt"` remains supported for compatibility, but new examples use the explicit `run` subcommand.
```

- [ ] **Step 2: Add CLI command overview**

Add this section after "常用交互命令":

```markdown
## CLI Surface

Explicit commands:

- `pico run [prompt...]`: run one prompt and exit.
- `pico repl`: start the interactive REPL.
- `pico status`: show local harness state without starting a model session.
- `pico doctor`: run readiness diagnostics, including provider connectivity.
- `pico doctor --offline`: run local diagnostics without provider connectivity checks.
- `pico config show`: show effective configuration and where each value came from.
- `pico runs list` / `pico runs show <run-id>`: inspect run artifacts.
- `pico sessions list` / `pico sessions show <session-id>`: inspect saved sessions.
- `pico checkpoints list` / `show` / `preview-restore` / `restore --apply` / `prune`: inspect and apply recovery actions.

Machine-readable output:

```bash
pico --format json status
pico --format json checkpoints list
```

Recovery commands remain preview-first. `restore` and `prune` only mutate files or checkpoint artifacts when `--apply` is present.
```

- [ ] **Step 3: Run documentation-adjacent checks**

Run:

```bash
uv run pytest tests/test_public_api_contract.py tests/test_recovery_cli.py tests/test_cli_commands.py tests/test_cli_diagnostics.py tests/test_cli_output.py tests/test_cli_parser.py -q
```

Expected: all listed tests pass.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document explicit pico cli surface"
```

## Task 10: Final Verification

**Files:**
- Test: full project test suite

- [ ] **Step 1: Run full tests**

Run:

```bash
uv run pytest tests -q
```

Expected: all tests pass.

- [ ] **Step 2: Run lint**

Run:

```bash
uv run ruff check pico tests scripts
```

Expected: no lint errors.

- [ ] **Step 3: Manually inspect help**

Run:

```bash
uv run pico help
uv run pico --help
```

Expected:

- examples appear near the top;
- implemented commands are grouped;
- reserved commands such as `providers list`, `tools list`, `schema show`, and `completion` do not appear as available commands.

- [ ] **Step 4: Manually inspect JSON output**

Run:

```bash
uv run pico --format json status
uv run pico --format json doctor --offline
```

Expected:

- stdout is parseable JSON;
- no human-only tip text appears in stdout;
- secrets are not printed.

- [ ] **Step 5: Commit any final test fixes**

If verification required small fixes, commit them:

```bash
git add pico tests README.md
git commit -m "fix: complete cli surface verification"
```

If no fixes were required, do not create an empty commit.
