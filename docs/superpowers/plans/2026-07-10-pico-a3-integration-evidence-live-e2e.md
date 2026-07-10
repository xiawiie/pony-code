# Pico A3 Integration, Evidence, and Live E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the completed A1 sensitive-data/safe-execution work and A2 recovery work as one integrated local trust baseline, publish reproducible local evidence, and run exactly one final real DeepSeek E2E without exposing credentials.

**Architecture:** A3 adds no security policy layer. It consumes A1's functional security and hardened subprocess interfaces plus A2's locked recovery and pending-review interfaces, then verifies them through three narrow adversarial integration suites, one existing perf harness extension, the existing five-turn live harness, and the existing evidence generators. Deterministic local tests remain the source of truth for security; the single paid DeepSeek run proves only final integration and native-tool/runtime contracts.

**Tech Stack:** Python 3.11+, stdlib, pytest, Ruff, existing Pico benchmark/evaluation modules, existing Anthropic-compatible DeepSeek client; no new dependencies.

## Global Constraints

- Authoritative design: `docs/superpowers/specs/2026-07-10-pico-security-trust-baseline-design.md` at commit `3848529` or later.
- A1 and A2 must be fully committed and their focused suites green before A3 starts.
- Use the frozen A1 interface names from `docs/superpowers/plans/2026-07-10-pico-a1-sensitive-data-safe-execution.md`. Do not reintroduce superseded lexical-root or executable-map names.
- Use A2's frozen CheckpointStore, ToolChangeRecorder, RecoveryManager, and recovery CLI interfaces listed below.
- Add no OS sandbox, secret vault, encryption layer, Provider registry, policy framework, pytest plugin, shell parser dependency, or machine-specific latency threshold.
- Direct tools and the automatic shell lane must fail closed for sensitive paths. An explicitly approved complex `shell=True` command remains a documented human-authorized escape hatch, not a sandboxed operation.
- Exact recovery blobs remain exact. Sensitive path/content must produce zero automatic recovery blob rather than a redacted restore payload.
- POSIX private files are 0600 and owned private directories are 0700. Permission checks never follow symlinks.
- A3 may repair only integration defects exposed by its tests, and each repair belongs in the existing A1 or A2 owner module.
- Every implementation task follows red-green-refactor, ends with focused green tests, and creates one intentional commit.
- All offline and deterministic gates must pass before any paid/network request.
- Run exactly one real DeepSeek E2E process in Task 8. Do not switch Provider and do not automatically retry if it fails.
- Never print, hash into evidence, serialize, stage, or commit an API key, request header, credential-bearing URL, or environment dictionary.
- Live E2E reports remain ignored under `benchmarks/live_e2e/results/`.

## Frozen Cross-Plan Interfaces Consumed by A3

A1 security signatures:

- `SensitiveDataBlockedError(RuntimeError)`
- `contains_secret_material(text, env=None, secret_env_names=None) -> bool`
- `redact_text(text, env=None, secret_env_names=None) -> str`
- `redact_artifact(value, key=None, env=None, secret_env_names=None)`
- `sanitize_provider_payload(system, messages, env=None, secret_env_names=None) -> tuple[str, list]`
- `sensitive_path_reason(raw_path) -> str`
- `is_sensitive_path(raw_path) -> bool`
- `require_regular_no_symlink(path, *, allow_missing=False) -> Path`
- `ensure_private_dir(path) -> Path`
- `ensure_private_file(path) -> Path`

A1 safe-subprocess signatures:

- `discover_lexical_repo_root(cwd) -> Path`
- `build_trusted_executables(workspace_root, *, env=None, names=()) -> dict[str, str]`
- `run_hardened_git(executable, args, *, cwd, timeout=5, check=False, text=False)`
- `run_hardened_rg(executable, args, *, cwd, timeout=20)`

A1 config and command signatures:

- `project_env_path(workspace_root) -> Path`
- `read_project_env(workspace_root, warn=True) -> dict[str, str]`
- `load_project_env(workspace_root, override=True, warn=True) -> dict[str, str]`
- `write_project_env_assignments(workspace_root, assignments) -> dict`
- `assess_command(command, workspace_root, executables=None) -> dict`

A2 CheckpointStore signatures:

- `mutation_lock()`
- `update_checkpoint_record(checkpoint_id, transform, *, expected_status=None)`
- `update_tool_change_record(tool_change_id, transform, *, expected_status=None)`
- `list_checkpoint_records(*, strict=False)`
- `list_tool_change_records(*, strict=False)`
- `quarantine_invalid_record(opaque_id, *, expected_raw_hash)`
- `list_quarantined_records()`

A2 ToolChangeRecorder signatures:

- `pending_recovery_reviews()`
- `resolve_pending(tool_change_id, *, reviewed_by, review_reason)`

A2 RecoveryManager signatures:

- `preview_restore(checkpoint_id)`
- `apply_restore(checkpoint_id)`
- `pending_restore_reviews()`
- `preview_restore_journal_resolution(checkpoint_id)`
- `resolve_restore_journal(checkpoint_id, *, expected_record_hash, reviewed_by, review_reason)`

A2 read-only Recovery Review inspection signature:

- `collect_recovery_review_items(store, workspace_root) -> dict`
- Result keys are exactly `tool_changes`, `restore_journals`, `invalid_records`, and `quarantined_records`.
- Inspection uses non-strict enumeration and returns only opaque `invalid_<hash>` IDs; mutation guards continue to use strict enumeration and fail closed.

Recovery CLI contracts consumed by A3:

```text
pico-cli checkpoints pending
pico-cli checkpoints resolve-pending <id>
pico-cli checkpoints resolve-pending <id> --apply
```

## File Responsibility Map

| File | A3 responsibility |
| --- | --- |
| `tests/test_security_integration.py` | One-sentinel Provider/session/artifact/CLI/log integration gate |
| `tests/test_shell_security_corpus.py` | Exact command outcome matrix and ToolExecutor runner-count gate |
| `tests/test_recovery_durability_e2e.py` | Journal crash, durability, reconciliation, mode, pending, and quarantine integration |
| `benchmarks/perf/bench_security_recovery.py` | Read-only redaction, command assessment, pending enumeration, and restore-preview latency smoke |
| `tests/test_perf_harness.py` | Freeze the new perf JSON shape and scenario names |
| `pico/cli_recovery.py`, `pico/cli_diagnostics.py` | Reuse one pending-review data builder in recovery CLI and doctor |
| `README.md`, `pico/cli_commands.py` | Truthful security, approval, recovery, and secret-input user contract |
| `benchmarks/live_e2e/run_live_session.py` | Active-artifact/key/mode checks on the existing five-turn harness |
| `benchmarks/live_e2e/tests/test_assertions.py` | Offline proof for all new live-harness checks |
| `benchmarks/live_e2e/README.md` | One-Provider, key-safe, no-sandbox live-gate boundary |
| `benchmarks/results/security-trust-baseline-2026-07-10/` | Fresh deterministic evidence and provenance |
| `docs/review-pack/README.md`, `docs/review-pack/dashboard.md` | Current A-stage evidence index and status |
| `.superpowers/sdd/progress.md` | Independent review and final verification ledger |

---

### Task 1: Add the Cross-Boundary Artifact Canary Gate

**Files:**
- Create: `tests/test_security_integration.py`
- Modify on failure only: `pico/security.py`, `pico/context_manager.py`, `pico/agent_loop.py`, `pico/runtime.py`, `pico/session_store.py`, `pico/run_store.py`, `pico/checkpoint_store.py`, `pico/cli_recovery.py`, `pico/cli_start.py`, `pico/providers/anthropic_compatible.py`

**Interfaces:**
- Consumes: A1 `redact_text()`, `redact_artifact()`, `contains_secret_material()`, `SensitiveDataBlockedError`, safe SessionStore/RunStore/CheckpointStore write boundaries, decoded Action rejection, and safe CLI inspection.
- Consumes: A2 terminal Tool Change and checkpoint record writers without changing their schemas.
- Produces: one deterministic integration suite proving the same canary cannot cross normal Provider, memory, disk, approval, verification, logging, or CLI observation boundaries.

- [ ] **Step 1: Create the shared test helpers and the failing Provider/session canary test**

Create `tests/test_security_integration.py` with these imports and helpers:

```python
import json
import os
from unittest.mock import Mock

import pytest

from pico.cli import main
from pico.providers.response import Response, StopReason
from pico.recovery_models import new_checkpoint_record
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.task_state import TaskState
from pico.workspace import WorkspaceContext


def _sentinel():
    return "ghp_" + "A" * 32


class CapturingClient:
    supports_native_tools = True
    supports_prompt_cache = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete_v2(self, **request):
        self.requests.append(request)
        return self.responses.pop(0)


def _agent(tmp_path, client, *, approval_policy="auto"):
    (tmp_path / "README.md").write_text("safe fixture\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    return Pico(
        model_client=client,
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy=approval_policy,
        secret_env_names=("PICO_TEST_TOKEN",),
    )


def _normal_artifact_files(root):
    for path in sorted((root / ".pico").rglob("*")):
        if not path.is_file():
            continue
        if "/sessions/backups/" in path.as_posix():
            continue
        yield path


def test_canary_is_absent_from_provider_session_and_normal_artifacts(
    tmp_path, monkeypatch
):
    secret = _sentinel()
    monkeypatch.setenv("PICO_TEST_TOKEN", secret)
    client = CapturingClient(
        [
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_canary_write",
                        "name": "write_file",
                        "input": {
                            "path": "safe.txt",
                            "content": "safe body\n",
                        },
                    }
                ],
            ),
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": secret}],
            ),
        ]
    )
    agent = _agent(tmp_path, client)

    answer = agent.ask("keep this private: " + secret)

    assert secret not in answer
    assert secret not in json.dumps(client.requests, ensure_ascii=False)
    assert secret not in json.dumps(agent.session, ensure_ascii=False)
    for path in _normal_artifact_files(tmp_path):
        assert secret.encode() not in path.read_bytes(), path
```

- [ ] **Step 2: Run the Provider/session test and confirm red**

Run: `uv run pytest tests/test_security_integration.py::test_canary_is_absent_from_provider_session_and_normal_artifacts -q`

Expected: FAIL at the first remaining unsanitized Provider, in-memory, or normal-artifact boundary. The test must never make a network call.

- [ ] **Step 3: Add old-artifact CLI, approval, verification, and backup-exception tests**

Append these tests:

```python
def test_cli_approval_and_verification_observations_hide_canary(
    tmp_path, monkeypatch, capsys
):
    secret = _sentinel()
    monkeypatch.setenv("PICO_TEST_TOKEN", secret)
    client = CapturingClient(
        [
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "done"}],
            )
        ]
    )
    agent = _agent(tmp_path, client, approval_policy="ask")
    state = TaskState.create(
        run_id="run_canary",
        task_id="task_canary",
        user_request=secret,
    )
    agent.run_store.start_run(state)
    agent.emit_trace(state, "canary", {"token": secret})
    agent.run_store.write_report(state, {"token": secret})
    checkpoint = new_checkpoint_record(
        "ckpt_canary",
        "turn",
        agent.session["id"],
        state.run_id,
        state.task_id,
        "",
        str(tmp_path.resolve()),
    )
    checkpoint["status"] = "applied"
    checkpoint["verification_evidence"] = [
        {
            "command": "python -m pytest",
            "stdout_tail": secret,
            "stderr_tail": secret,
        }
    ]
    agent.checkpoint_store.write_checkpoint_record(checkpoint)

    prompts = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or "n",
    )
    assert agent.approve(
        "run_shell",
        {"command": "printf safe", "token": secret},
    ) is False
    assert secret not in "".join(prompts)

    assert (
        main(
            [
                "--cwd",
                str(tmp_path),
                "--format",
                "json",
                "checkpoints",
                "show",
                "ckpt_canary",
            ]
        )
        == 0
    )
    assert secret not in capsys.readouterr().out


def test_secret_bearing_tool_action_is_blocked_before_runner(
    tmp_path, monkeypatch
):
    secret = _sentinel()
    monkeypatch.setenv("PICO_TEST_TOKEN", secret)
    client = CapturingClient(
        [
            Response(
                stop_reason=StopReason.TOOL_USE,
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_secret_action",
                        "name": "write_file",
                        "input": {
                            "path": "secret-action.txt",
                            "content": secret,
                        },
                    }
                ],
            ),
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "blocked"}],
            ),
        ]
    )
    agent = _agent(tmp_path, client)
    runner = Mock(return_value="must not run")
    agent.tools["write_file"]["run"] = runner

    answer = agent.ask("create a safe file")

    assert answer == "blocked"
    runner.assert_not_called()
    assert not (tmp_path / "secret-action.txt").exists()
    assert secret not in json.dumps(client.requests, ensure_ascii=False)
    assert secret not in json.dumps(agent.session, ensure_ascii=False)
    for path in _normal_artifact_files(tmp_path):
        assert secret.encode() not in path.read_bytes(), path


def test_private_migration_backup_is_the_only_exact_canary_exception(
    tmp_path, monkeypatch
):
    if os.name != "posix":
        pytest.skip("POSIX permission assertion")
    secret = _sentinel()
    monkeypatch.setenv("PICO_TEST_TOKEN", secret)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    session_id = "legacy-canary"
    legacy_path = store.root / (session_id + ".json")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "id": session_id,
                "history": [{"role": "user", "content": secret}],
            }
        ),
        encoding="utf-8",
    )
    from pico.security import redact_artifact

    store.set_redactor(
        lambda value: redact_artifact(
            value,
            secret_env_names=("PICO_TEST_TOKEN",),
        )
    )

    safe = store.load(session_id)
    backups = sorted((store.root / "backups").glob("*.json"))

    assert secret not in json.dumps(safe, ensure_ascii=False)
    assert len(backups) == 1
    assert secret.encode() in backups[0].read_bytes()
    assert backups[0].stat().st_mode & 0o777 == 0o600
    for path in _normal_artifact_files(tmp_path):
        assert secret.encode() not in path.read_bytes(), path
```

- [ ] **Step 4: Add the HTTP error and logging canary test**

Append:

```python
def test_provider_error_log_cli_and_run_artifacts_hide_canary(
    tmp_path, monkeypatch, caplog, capsys
):
    secret = _sentinel()
    monkeypatch.setenv("PICO_TEST_TOKEN", secret)

    class FailingClient:
        supports_native_tools = True
        supports_prompt_cache = False

        def complete_v2(self, **request):
            raise RuntimeError(
                "HTTP 500 body=" + secret
                + " url=https://user:" + secret
                + "@example.invalid/v1?api_key=" + secret
            )

    agent = _agent(tmp_path, FailingClient())

    with pytest.raises(RuntimeError):
        agent.ask("trigger provider error")

    captured = capsys.readouterr()
    observed = caplog.text + captured.out + captured.err
    assert secret not in observed
    for path in _normal_artifact_files(tmp_path):
        assert secret.encode() not in path.read_bytes(), path
```

- [ ] **Step 5: Make only owner-boundary repairs and run the full canary slice**

Fix each failure in its existing A1/A2 owner. Do not add a second sanitizer or a test-only bypass in A3.

Run: `uv run pytest tests/test_security.py tests/test_security_integration.py tests/test_safety_invariants.py tests/test_artifact_security.py tests/test_secret_boundaries.py -q`

Expected: all tests pass; every blocked secret ToolAction has Provider/runner call count zero; only the explicit private migration backup contains exact canary bytes.

- [ ] **Step 6: Run Ruff and commit**

Run: `uv run ruff check tests/test_security_integration.py pico/security.py pico/context_manager.py pico/agent_loop.py pico/runtime.py pico/session_store.py pico/run_store.py pico/checkpoint_store.py pico/cli_recovery.py pico/cli_start.py pico/providers/anthropic_compatible.py`

Expected: no diagnostics.

```bash
git add tests/test_security_integration.py pico/security.py pico/context_manager.py pico/agent_loop.py pico/runtime.py pico/session_store.py pico/run_store.py pico/checkpoint_store.py pico/cli_recovery.py pico/cli_start.py pico/providers/anthropic_compatible.py
git commit -m "test(security): add end-to-end artifact canary gate"
```

---

### Task 2: Lock the Shell and Trusted-Executable Adversarial Corpus

**Files:**
- Create: `tests/test_shell_security_corpus.py`
- Modify on failure only: `pico/recovery_policy.py`, `pico/safe_subprocess.py`, `pico/tool_executor.py`, `pico/tools.py`

**Interfaces:**
- Consumes: A1 `assess_command(command, workspace_root, executables=None)`, `build_trusted_executables()`, `run_hardened_git()`, and `run_hardened_rg()`.
- Consumes: ToolExecutor's complete `command_approval` metadata with `decision`, `reason`, `mode`, `outcome`, `runner_executed`, and `execution_mode`.
- Produces: one exact table proving that no unknown, complex, privileged, interpreter, path-binary, external-helper, or sensitive-path sample reaches the automatic runner.

- [ ] **Step 1: Create the exact command-assessment corpus**

Create `tests/test_shell_security_corpus.py`:

```python
from unittest.mock import Mock

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.recovery_policy import assess_command


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


ALLOW_ARGV = (
    "pwd",
    "ls",
    "ls -1 README.md",
    "stat README.md",
    "file --brief README.md",
    "wc -l README.md",
    "git status --short",
    "git rev-parse HEAD",
    "git branch --show-current",
    "git worktree list",
    "git ls-files",
)

ASK_ARGV = (
    "unknown-binary --version",
    "python -m pytest -q",
    "bash -c 'pwd && pwd'",
    "sudo true",
    "systemctl status sshd",
    "npm test",
    "curl https://example.invalid",
    "./ls",
    "/bin/ls",
    "date -s 2030-01-01",
    "rg --pre cat token .",
    "git diff --ext-diff",
    "git log -1",
    "env pwd",
    "xargs printf",
)

ASK_SHELL = (
    "pwd | wc -l",
    "pwd && pwd",
    "pwd || true",
    "pwd; pwd",
    "printf x > out.txt",
    "if true; then pwd; fi",
    "$(pwd)",
    "`pwd`",
    "cat <<EOF\nbody\nEOF",
    "find . -exec printf x ;",
)

REJECT = (
    "cat .env",
    "printf x > .env",
    "ls .ssh",
    "cat .pico/sessions/session.json",
)


@pytest.mark.parametrize("command", ALLOW_ARGV)
def test_exact_auto_grammar_is_allow_argv(tmp_path, command):
    (tmp_path / "README.md").write_text("safe\n", encoding="utf-8")
    assessment = assess_command(command, tmp_path)
    assert assessment["decision"] == "allow"
    assert assessment["execution_mode"] == "argv"
    assert assessment["argv"]


@pytest.mark.parametrize("command", ASK_ARGV)
def test_simple_risky_or_unknown_commands_are_ask_argv(tmp_path, command):
    assessment = assess_command(command, tmp_path)
    assert assessment["decision"] == "ask"
    assert assessment["execution_mode"] == "argv"
    assert assessment["argv"]


@pytest.mark.parametrize("command", ASK_SHELL)
def test_shell_grammar_is_ask_shell(tmp_path, command):
    assessment = assess_command(command, tmp_path)
    assert assessment["decision"] == "ask"
    assert assessment["execution_mode"] == "shell"


@pytest.mark.parametrize("command", REJECT)
def test_literal_sensitive_targets_are_hard_reject(tmp_path, command):
    assessment = assess_command(command, tmp_path)
    assert assessment["decision"] == "reject"
```

- [ ] **Step 2: Run the pure assessment matrix and confirm red**

Run: `uv run pytest tests/test_shell_security_corpus.py -q`

Expected: failures identify every grammar, option, path, or execution-mode mismatch. No subprocess is invoked by this pure assessment slice.

- [ ] **Step 3: Add ToolExecutor runner, approval, and metadata assertions**

Append:

```python
@pytest.mark.parametrize("command", ASK_ARGV + ASK_SHELL + REJECT)
def test_auto_mode_never_calls_runner_for_non_allow_command(
    tmp_path, command
):
    agent = build_agent(tmp_path, [], approval_policy="auto")
    runner = Mock(return_value="must not run")
    agent.tools["run_shell"]["run"] = runner

    result = agent.execute_tool(
        "run_shell",
        {"command": command, "timeout": 20},
    )

    assert result.metadata["tool_status"] == "rejected"
    approval = result.metadata["command_approval"]
    assert approval["runner_executed"] is False
    assert approval["outcome"] in {"blocked", "denied"}
    assert approval["execution_mode"] in {"argv", "shell"}
    runner.assert_not_called()


def test_ask_mode_approved_simple_command_stays_argv(
    tmp_path, monkeypatch
):
    agent = build_agent(tmp_path, [], approval_policy="ask")
    monkeypatch.setattr(agent, "approve", lambda name, args: True)
    runner = Mock(
        return_value="exit_code: 0\nstdout:\npassed\nstderr:\n(empty)"
    )
    agent.tools["run_shell"]["run"] = runner

    result = agent.execute_tool(
        "run_shell",
        {"command": "python -m pytest -q", "timeout": 20},
    )

    approval = result.metadata["command_approval"]
    assert approval["outcome"] == "approved"
    assert approval["runner_executed"] is True
    assert approval["execution_mode"] == "argv"
    runner.assert_called_once()


def test_read_only_and_never_modes_do_not_prompt_or_run(
    tmp_path, monkeypatch
):
    configurations = (
        {"approval_policy": "never"},
        {"approval_policy": "ask", "read_only": True},
    )
    for kwargs in configurations:
        agent = build_agent(tmp_path, [], **kwargs)
        approve = Mock(return_value=True)
        runner = Mock(return_value="must not run")
        monkeypatch.setattr(agent, "approve", approve)
        agent.tools["run_shell"]["run"] = runner

        result = agent.execute_tool(
            "run_shell",
            {"command": "python -m pytest -q", "timeout": 20},
        )

        assert result.metadata["tool_status"] == "rejected"
        approve.assert_not_called()
        runner.assert_not_called()
```

- [ ] **Step 4: Add trusted executable and hostile configuration tests**

Append:

```python
def test_workspace_binary_and_relative_path_never_win_trust(
    tmp_path, monkeypatch
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_git.chmod(0o755)
    unsafe_path = ".:" + str(fake_bin) + ":/usr/bin:/bin"

    from pico.safe_subprocess import build_trusted_executables

    trusted = build_trusted_executables(
        tmp_path,
        env={"PATH": unsafe_path},
        names=("git",),
    )

    assert trusted.get("git") != str(fake_git)


def test_hardened_git_and_rg_ignore_executable_repo_config(
    tmp_path, monkeypatch
):
    from pico.safe_subprocess import (
        build_trusted_executables,
        run_hardened_git,
        run_hardened_rg,
    )

    calls = []

    def fake_run(argv, **kwargs):
        calls.append((tuple(argv), dict(kwargs)))
        return type(
            "Completed",
            (),
            {"returncode": 0, "stdout": b"", "stderr": b""},
        )()

    monkeypatch.setattr("pico.safe_subprocess.subprocess.run", fake_run)
    executables = build_trusted_executables(
        tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "RIPGREP_CONFIG_PATH": str(tmp_path / "rg.conf"),
            "GIT_CONFIG_COUNT": "1",
        },
        names=("git", "rg"),
    )
    if "git" in executables:
        run_hardened_git(
            executables["git"],
            ["status", "--short"],
            cwd=tmp_path,
        )
    if "rg" in executables:
        run_hardened_rg(
            executables["rg"],
            ["token", "."],
            cwd=tmp_path,
        )

    for argv, kwargs in calls:
        joined = " ".join(argv)
        env = kwargs["env"]
        assert "core.fsmonitor=false" in joined or argv[0].endswith("rg")
        assert "RIPGREP_CONFIG_PATH" not in env
        assert all(not key.startswith("GIT_") for key in env)
```

- [ ] **Step 5: Repair only A1 owners and run the complete shell gate**

Run: `uv run pytest tests/test_recovery_policy.py tests/test_tool_executor.py tests/test_tools.py tests/test_shell_security_corpus.py tests/test_bootstrap_read_safety.py tests/test_workspace_observer.py -q`

Expected: all pass; every automatic ask/reject sample has runner count zero; approved simple-risk commands remain `argv`; only approved real shell grammar reports `shell`.

- [ ] **Step 6: Run Ruff and commit**

Run: `uv run ruff check tests/test_shell_security_corpus.py pico/recovery_policy.py pico/safe_subprocess.py pico/tool_executor.py pico/tools.py`

Expected: no diagnostics.

```bash
git add tests/test_shell_security_corpus.py pico/recovery_policy.py pico/safe_subprocess.py pico/tool_executor.py pico/tools.py
git commit -m "test(shell): lock fail-closed command corpus"
```

---

### Task 3: Add Recovery Crash, Durability, Quarantine, and Performance Gates

**Files:**
- Create: `tests/test_recovery_durability_e2e.py`
- Create: `benchmarks/perf/bench_security_recovery.py`
- Modify: `tests/test_perf_harness.py`
- Modify: `benchmarks/perf/README.md`
- Modify on failure only: `pico/recovery_manager.py`, `pico/recovery_checkpoint_writer.py`, `pico/checkpoint_store.py`, `pico/tool_change_recorder.py`, `pico/cli_recovery.py`

**Interfaces:**
- Consumes: all frozen A2 store/recorder/manager interfaces, journal `pre_state`/`planned_post_state` tuples, `_apply_intent()` and `_fsync_target_parent()` test seams from A2 Tasks 9–10, and the three Recovery Review CLI commands.
- Produces: a CLI-level crash/reconciliation test and a four-scenario read-only perf smoke. It does not add a second recovery state machine.

- [ ] **Step 1: Create concrete recovery fixtures and failing crash tests**

Create `tests/test_recovery_durability_e2e.py`:

```python
import json
import os

import pytest

from pico.checkpoint_store import CheckpointStore
from pico.cli import main
from pico.recovery_checkpoint_writer import RecoveryCheckpointWriter
from pico.recovery_manager import RecoveryManager
from pico.recovery_models import new_checkpoint_record


def _source_checkpoint(store, root, names=("one.txt",)):
    entries = []
    targets = []
    for index, name in enumerate(names):
        before_bytes = f"before-{index}".encode()
        after_bytes = f"after-{index}".encode()
        before = store.write_blob(before_bytes)
        after = store.write_blob(after_bytes)
        target = root / name
        target.write_bytes(after_bytes)
        target.chmod(0o640)
        targets.append(target)
        entries.append(
            {
                "path": name,
                "change_kind": "modified",
                "snapshot_eligible": True,
                "ineligible_reason": "",
                "before_exists": True,
                "before_blob_ref": before["blob_ref"],
                "before_hash": before["content_hash"],
                "before_mode": 0o600,
                "after_exists": True,
                "after_blob_ref": after["blob_ref"],
                "after_hash": after["content_hash"],
                "after_mode": 0o640,
                "expected_current_hash": after["content_hash"],
                "source_tool_change_ids": [],
            }
        )
    record = new_checkpoint_record(
        "ckpt_source",
        "turn",
        "session",
        "run",
        "turn",
        "",
        str(root.resolve()),
    )
    record["file_entries"] = entries
    record["tool_change_ids"] = []
    record["missing_tool_change_ids"] = []
    store.write_checkpoint_record(record)
    return targets


def _manager(store, root):
    return RecoveryManager(
        store,
        root,
        checkpoint_writer=RecoveryCheckpointWriter(store, root),
    )


def _applying_journal(store):
    records = [
        record
        for record in store.list_checkpoint_records()
        if record.get("checkpoint_type") == "restore"
        and record.get("status") == "applying"
    ]
    assert len(records) == 1
    return records[0]


def test_replace_then_crash_before_outcome_reconciles_applied_unconfirmed(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    target = _source_checkpoint(store, tmp_path)[0]
    manager = _manager(store, tmp_path)
    real_update = store.update_checkpoint_record

    def crash_before_outcome(checkpoint_id, transform, *, expected_status=None):
        current = store.load_checkpoint_record(checkpoint_id)
        candidate = transform(dict(current))
        entries = candidate.get("restore_provenance", {}).get("entries", [])
        if any(entry.get("outcome") == "applied" for entry in entries):
            raise KeyboardInterrupt
        return real_update(
            checkpoint_id,
            transform,
            expected_status=expected_status,
        )

    monkeypatch.setattr(
        store,
        "update_checkpoint_record",
        crash_before_outcome,
    )

    with pytest.raises(KeyboardInterrupt):
        manager.apply_restore("ckpt_source")

    journal = _applying_journal(store)
    assert target.read_bytes() == b"before-0"
    preview = manager.preview_restore_journal_resolution(
        journal["checkpoint_id"]
    )
    assert preview["entries"][0]["classification"] == "applied_unconfirmed"
    assert store.load_checkpoint_record(journal["checkpoint_id"]) == journal


def test_target_parent_fsync_failure_is_uncertain_partial(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    _source_checkpoint(store, tmp_path)
    manager = _manager(store, tmp_path)

    def fail_parent_fsync(path):
        raise OSError("target parent fsync failed")

    monkeypatch.setattr(
        manager,
        "_fsync_target_parent",
        fail_parent_fsync,
    )
    result = manager.apply_restore("ckpt_source")
    journal = store.load_checkpoint_record(
        result["restore_checkpoint_id"]
    )

    assert result["status"] == "partial"
    assert journal["status"] == "partial"
    assert journal["restore_provenance"]["entries"][0]["outcome"] == "uncertain"
```

- [ ] **Step 2: Run the crash tests and confirm red**

Run: `uv run pytest tests/test_recovery_durability_e2e.py -q`

Expected: FAIL if a crash has no durable intent, preview mutates the journal, parent fsync is ordered incorrectly, or uncertain target state is reported as success.

- [ ] **Step 3: Add multi-file, mode, pending, and quarantine integration tests**

Append:

```python
def test_second_file_failure_records_partial_and_proven_undo(
    tmp_path, monkeypatch
):
    store = CheckpointStore(tmp_path)
    targets = _source_checkpoint(
        store,
        tmp_path,
        names=("one.txt", "two.txt"),
    )
    manager = _manager(store, tmp_path)
    real_apply = manager._apply_intent
    calls = {"count": 0}

    def fail_second(restore_checkpoint_id, intent):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("second target failed")
        return real_apply(restore_checkpoint_id, intent)

    monkeypatch.setattr(manager, "_apply_intent", fail_second)
    result = manager.apply_restore("ckpt_source")
    journal = store.load_checkpoint_record(
        result["restore_checkpoint_id"]
    )

    assert result["status"] == "partial"
    assert targets[0].read_bytes() == b"before-0"
    assert targets[1].read_bytes() == b"after-1"
    assert len(journal["file_entries"]) == 1
    review_preview = manager.preview_restore(journal["checkpoint_id"])
    assert review_preview["status"] == "review_required"
    assert any(
        entry["reason"] == "partial_review_required"
        for entry in review_preview["entries"]
    )
    blocked = manager.apply_restore(journal["checkpoint_id"])
    assert blocked["status"] == "blocked"
    assert targets[0].read_bytes() == b"before-0"
    assert targets[1].read_bytes() == b"after-1"
    preview_code = main([
        "--cwd", str(tmp_path), "checkpoints", "resolve-pending", journal["checkpoint_id"],
    ])
    assert preview_code == 0
    review_code = main([
        "--cwd", str(tmp_path), "checkpoints", "resolve-pending", journal["checkpoint_id"], "--apply",
    ])
    assert review_code == 0
    undo = manager.apply_restore(journal["checkpoint_id"])
    assert undo["status"] == "applied"
    assert targets[0].read_bytes() == b"after-0"


def test_mode_round_trip_survives_restore_and_undo(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX mode assertion")
    store = CheckpointStore(tmp_path)
    target = _source_checkpoint(store, tmp_path)[0]
    manager = _manager(store, tmp_path)

    restored = manager.apply_restore("ckpt_source")
    assert target.stat().st_mode & 0o777 == 0o600
    manager.apply_restore(restored["restore_checkpoint_id"])
    assert target.stat().st_mode & 0o777 == 0o640


def test_invalid_mutation_record_is_previewed_then_privately_quarantined(
    tmp_path, capsys
):
    store = CheckpointStore(tmp_path)
    raw = b"{private-invalid-evidence"
    source = store.tool_changes_dir / "tc_invalid.json"
    source.write_bytes(raw)

    assert (
        main(
            [
                "--cwd",
                str(tmp_path),
                "--format",
                "json",
                "checkpoints",
                "pending",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    invalid_records = payload["data"]["invalid_records"]
    assert len(invalid_records) == 1
    assert invalid_records[0]["status"] == "invalid_record"
    assert "private-invalid-evidence" not in json.dumps(payload)
    invalid_id = invalid_records[0]["opaque_id"]
    assert invalid_id.startswith("invalid_")
    assert "tc_invalid" not in json.dumps(invalid_records)

    assert (
        main(
            [
                "--cwd",
                str(tmp_path),
                "checkpoints",
                "resolve-pending",
                invalid_id,
                "--apply",
            ]
        )
        == 0
    )
    quarantined = store.list_quarantined_records()
    assert len(quarantined) == 1
    assert quarantined[0]["raw_hash"]
    assert raw not in json.dumps(quarantined).encode()
    assert not source.exists()
    raw_paths = list((store.root / "quarantine").rglob("*.raw"))
    assert len(raw_paths) == 1
    assert raw_paths[0].read_bytes() == raw
    if os.name == "posix":
        assert raw_paths[0].stat().st_mode & 0o777 == 0o600
```

- [ ] **Step 4: Repair only A2 owners and run the complete recovery slice**

Run: `uv run pytest tests/test_checkpoint_store_security.py tests/test_checkpoint_store_durability.py tests/test_recovery_checkpoint_writer.py tests/test_recovery_manager.py tests/test_recovery_journal.py tests/test_tool_change_recorder.py tests/test_tool_executor_mutation_lock.py tests/test_recovery_cli.py tests/test_recovery_e2e.py tests/test_recovery_durability_e2e.py -q`

Expected: all pass; pre-state/journal durability failures cause zero first mutation; post-target uncertainty is `partial`; preview is read-only; quarantine preserves raw bytes privately.

- [ ] **Step 5: Create the four-scenario perf smoke**

Create `benchmarks/perf/bench_security_recovery.py`:

```python
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from benchmarks.perf.harness import bench
from pico.checkpoint_store import CheckpointStore
from pico.recovery_manager import RecoveryManager
from pico.recovery_models import new_checkpoint_record
from pico.recovery_policy import assess_command
from pico.security import redact_artifact
from pico.tool_change_recorder import ToolChangeRecorder


SCENARIO_NAMES = (
    "security/redact_artifact/100",
    "shell/assess_corpus/50",
    "recovery/pending_reviews/200",
    "recovery/preview/100",
)


def _restore_fixture(root, count):
    store = CheckpointStore(root)
    entries = []
    for index in range(count):
        name = f"file-{index}.txt"
        before_bytes = f"before-{index}".encode()
        after_bytes = f"after-{index}".encode()
        before = store.write_blob(before_bytes)
        after = store.write_blob(after_bytes)
        (root / name).write_bytes(after_bytes)
        entries.append(
            {
                "path": name,
                "change_kind": "modified",
                "snapshot_eligible": True,
                "ineligible_reason": "",
                "before_exists": True,
                "before_blob_ref": before["blob_ref"],
                "before_hash": before["content_hash"],
                "before_mode": 0o600,
                "after_exists": True,
                "after_blob_ref": after["blob_ref"],
                "after_hash": after["content_hash"],
                "after_mode": 0o600,
                "expected_current_hash": after["content_hash"],
                "source_tool_change_ids": [],
            }
        )
    record = new_checkpoint_record(
        "ckpt_perf",
        "turn",
        "session",
        "run",
        "turn",
        "",
        str(root.resolve()),
    )
    record["file_entries"] = entries
    record["tool_change_ids"] = []
    record["missing_tool_change_ids"] = []
    store.write_checkpoint_record(record)
    return store, RecoveryManager(store, root)


def main():
    secrets = {
        f"PICO_TOKEN_{index}": f"ghp_{index:032d}"
        for index in range(100)
    }
    artifact = {
        "items": list(secrets.values()),
        "nested": [{"token": value} for value in secrets.values()],
    }
    commands = ["pwd", "python -m pytest -q", "cat .env", "pwd | wc -l"]
    command_batch = [commands[index % len(commands)] for index in range(50)]

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "README.md").write_text("safe\n", encoding="utf-8")
        store, manager = _restore_fixture(root, 100)
        recorder = ToolChangeRecorder(store, owner_id="perf-owner")
        for index in range(200):
            recorder.start(
                "",
                f"turn-{index}",
                "write_file",
                "workspace_write",
                {"path": f"pending-{index}.txt"},
            )

        scenarios = [
            bench(
                SCENARIO_NAMES[0],
                lambda: redact_artifact(
                    artifact,
                    env=secrets,
                    secret_env_names=tuple(secrets),
                ),
                iterations=20,
            ),
            bench(
                SCENARIO_NAMES[1],
                lambda: [
                    assess_command(command, root)
                    for command in command_batch
                ],
                iterations=20,
            ),
            bench(
                SCENARIO_NAMES[2],
                recorder.pending_recovery_reviews,
                iterations=20,
            ),
            bench(
                SCENARIO_NAMES[3],
                lambda: manager.preview_restore("ckpt_perf"),
                iterations=20,
            ),
        ]
    print(json.dumps({"scenarios": scenarios}, indent=2))


if __name__ == "__main__":
    main()
```

Add to `tests/test_perf_harness.py`:

```python
def test_security_recovery_scenario_names_are_stable():
    from benchmarks.perf.bench_security_recovery import SCENARIO_NAMES

    assert SCENARIO_NAMES == (
        "security/redact_artifact/100",
        "shell/assess_corpus/50",
        "recovery/pending_reviews/200",
        "recovery/preview/100",
    )
```

Add the command and the no-threshold boundary to `benchmarks/perf/README.md`.

- [ ] **Step 6: Run and validate the perf smoke**

```bash
uv run pytest tests/test_perf_harness.py -q
uv run python -m benchmarks.perf.bench_security_recovery > /tmp/pico-a3-security-recovery-perf.json
uv run python -c 'import json; payload=json.load(open("/tmp/pico-a3-security-recovery-perf.json")); expected=["security/redact_artifact/100","shell/assess_corpus/50","recovery/pending_reviews/200","recovery/preview/100"]; assert [item["name"] for item in payload["scenarios"]]==expected; assert all(item["min_ns"]>0 and item["p95_ns"]>=item["median_ns"]>=item["min_ns"] for item in payload["scenarios"]); print("4 security/recovery perf scenarios valid")'
```

Expected: pytest passes and the final command prints `4 security/recovery perf scenarios valid`. No wall-time threshold is asserted.

- [ ] **Step 7: Run Ruff and commit**

Run: `uv run ruff check tests/test_recovery_durability_e2e.py benchmarks/perf/bench_security_recovery.py tests/test_perf_harness.py pico/recovery_manager.py pico/recovery_checkpoint_writer.py pico/checkpoint_store.py pico/tool_change_recorder.py pico/cli_recovery.py`

Expected: no diagnostics.

```bash
git add tests/test_recovery_durability_e2e.py benchmarks/perf/bench_security_recovery.py benchmarks/perf/README.md tests/test_perf_harness.py pico/recovery_manager.py pico/recovery_checkpoint_writer.py pico/checkpoint_store.py pico/tool_change_recorder.py pico/cli_recovery.py
git commit -m "test(recovery): add crash durability and perf gates"
```

---

### Task 4: Integrate Doctor, Help, and Recovery Review Documentation

**Files:**
- Modify: `pico/cli_recovery.py`
- Modify: `pico/cli_diagnostics.py`
- Modify: `pico/cli_commands.py`
- Modify: `README.md`
- Modify: `tests/test_cli_commands.py`
- Modify: `tests/test_cli_diagnostics.py`
- Modify: `tests/test_recovery_cli.py`

**Interfaces:**
- Consumes: A1 exact-root project env, private modes, trusted executable map, and A2 `collect_recovery_review_items(store, workspace_root)` non-strict inspection data.
- Produces: stable `doctor.data.security`; truthful root help and README. It does not create a second pending collector.

- [ ] **Step 1: Add failing recovery-data and doctor contract tests**

Add to `tests/test_recovery_cli.py`:

```python
def test_collect_recovery_review_items_has_stable_shape(tmp_path):
    from pico.checkpoint_store import CheckpointStore
    from pico.cli_recovery import collect_recovery_review_items

    payload = collect_recovery_review_items(CheckpointStore(tmp_path), tmp_path)

    assert payload == {
        "tool_changes": [],
        "restore_journals": [],
        "invalid_records": [],
        "quarantined_records": [],
    }
```

Add to `tests/test_cli_diagnostics.py`:

```python
def test_doctor_json_exposes_safe_security_contract(
    tmp_path, monkeypatch, capsys
):
    from pico.cli import main

    (tmp_path / ".env").write_text(
        "PICO_PROVIDER=deepseek\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").chmod(0o600)
    monkeypatch.setattr(
        "pico.cli_diagnostics.build_trusted_executables",
        lambda root, env=None, names=(): {
            "git": "/usr/bin/git",
            "rg": "/usr/bin/rg",
        },
    )

    code = main(
        [
            "--cwd",
            str(tmp_path),
            "--format",
            "json",
            "doctor",
            "--offline",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    security = payload["data"]["security"]

    assert code == 0
    assert security == {
        "status": "ok",
        "project_env": {"status": "ok", "mode": "0600"},
        "private_storage": {"status": "missing"},
        "trusted_executables": {"status": "ok", "missing": []},
        "recovery_review": {
            "pending_count": 0,
            "applying_count": 0,
            "unreviewed_partial_count": 0,
            "invalid_mutation_count": 0,
        },
    }
    assert "PICO_PROVIDER=deepseek" not in json.dumps(payload)
```

Add a second doctor test that seeds one pending Tool Change and asserts `security.status == "review_required"` and `pending_count == 1`.

- [ ] **Step 2: Run focused CLI tests and confirm red**

Run: `uv run pytest tests/test_cli_commands.py tests/test_cli_diagnostics.py tests/test_recovery_cli.py -q`

Expected: FAIL because the shared collector and `security` doctor section are missing or the help contract is incomplete.

- [ ] **Step 3: Reuse the A2 read-only inspection collector**

Make `checkpoints pending` and doctor call `collect_recovery_review_items(store, workspace_root)` directly. The function non-strictly enumerates valid review items plus malformed/invalid records as opaque IDs; it must not call strict `pending_recovery_reviews()` after encountering malformed bytes. Strict enumeration remains confined to ToolExecutor/RecoveryManager mutation guards.

- [ ] **Step 4: Add the stable doctor security section**

In `pico/cli_diagnostics.py`, import `stat`, `build_trusted_executables`, `project_env_path`, and `collect_recovery_review_items`. Add helpers that use `lstat()` and never read secret values:

```python
def _mode_text(path):
    if os.name != "posix":
        return ""
    try:
        return format(stat.S_IMODE(path.lstat().st_mode), "04o")
    except FileNotFoundError:
        return ""


def _private_storage_status(pico_root):
    try:
        root_mode = pico_root.lstat().st_mode
    except FileNotFoundError:
        return {"status": "missing"}
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        return {"status": "review_required"}

    paths = [pico_root]
    for current, dirnames, filenames in os.walk(
        pico_root,
        followlinks=False,
    ):
        base = Path(current)
        paths.extend(base / name for name in dirnames + filenames)
    for path in paths:
        try:
            mode = path.lstat().st_mode
        except OSError:
            return {"status": "review_required"}
        if stat.S_ISLNK(mode):
            return {"status": "review_required"}
        if stat.S_ISDIR(mode):
            expected_mode = 0o700
        elif stat.S_ISREG(mode):
            expected_mode = 0o600
        else:
            return {"status": "review_required"}
        if os.name == "posix" and stat.S_IMODE(mode) != expected_mode:
            return {"status": "review_required"}
    return {"status": "ok"}


def _security_diagnostics(root):
    root = Path(root)
    env_path = project_env_path(root)
    try:
        env_info = env_path.lstat()
    except FileNotFoundError:
        project_env = {"status": "missing", "mode": ""}
    else:
        env_ok = (
            stat.S_ISREG(env_info.st_mode)
            and (os.name != "posix" or _mode_text(env_path) == "0600")
        )
        project_env = {
            "status": "ok" if env_ok else "review_required",
            "mode": _mode_text(env_path),
        }

    pico_root = root / ".pico"
    private_storage = _private_storage_status(pico_root)
    trusted = build_trusted_executables(
        root,
        env=os.environ,
        names=("git", "rg"),
    )
    missing = sorted({"git", "rg"} - set(trusted))
    reviews = collect_recovery_review_items(CheckpointStore(root), root)
    recovery_review = {
        "pending_count": len(reviews["tool_changes"]),
        "applying_count": sum(
            item.get("status") == "applying"
            for item in reviews["restore_journals"]
        ),
        "unreviewed_partial_count": sum(
            item.get("status") == "partial"
            and not item.get("reviewed_at")
            for item in reviews["restore_journals"]
        ),
        "invalid_mutation_count": len(reviews["invalid_records"]),
    }
    needs_review = (
        project_env["status"] == "review_required"
        or private_storage["status"] == "review_required"
        or bool(missing)
        or recovery_review["pending_count"] > 0
        or recovery_review["applying_count"] > 0
        or recovery_review["unreviewed_partial_count"] > 0
        or recovery_review["invalid_mutation_count"] > 0
    )
    return {
        "status": "review_required" if needs_review else "ok",
        "project_env": project_env,
        "private_storage": private_storage,
        "trusted_executables": {
            "status": "ok" if not missing else "degraded",
            "missing": missing,
        },
        "recovery_review": recovery_review,
    }
```

Add `"security": _security_diagnostics(root)` to `collect_doctor()`. Extend `_render_doctor()` with status, env mode, private storage, trusted executables, and the four recovery counts. Do not print env values, invalid raw bytes, quarantine bytes, key length, or key digest.

- [ ] **Step 5: Freeze root help and README truth**

In `pico/cli_commands.py`, update root help so `init` says non-secret provider configuration, `config` names `set-secret`, and `checkpoints` names pending/review.

Add a `Security and recovery boundaries` section to `README.md` containing all of these literal commands and guarantees:

```text
pico-cli --cwd <repo> config set-secret PICO_DEEPSEEK_API_KEY
pico-cli --cwd <repo> config set-secret PICO_DEEPSEEK_API_KEY --stdin
pico-cli --cwd <repo> checkpoints pending
pico-cli --cwd <repo> checkpoints resolve-pending <id>
pico-cli --cwd <repo> checkpoints resolve-pending <id> --apply
```

The section must state:

- secret values are never accepted in argv;
- `.env` and private Pico files are 0600, owned directories are 0700 on POSIX;
- direct tools and automatic shell reject sensitive paths;
- approved complex shell is a human-authorized escape hatch;
- Pico does not provide an OS sandbox, encryption, Vault, or network isolation;
- restore is single-file atomic, multi-file journaled, and partial/applying states require Recovery Review;
- resolution is preview-first and invalid evidence is privately quarantined rather than deleted.

Add help assertions to `tests/test_cli_commands.py` for `set-secret`, `pending`, `approval`, and `no OS sandbox`.

- [ ] **Step 6: Run CLI contract commands**

```bash
uv run pytest tests/test_cli_commands.py tests/test_cli_diagnostics.py tests/test_recovery_cli.py -q
uv run pico-cli help | rg 'config|checkpoints|approval'
uv run pico-cli --cwd . --format json doctor --offline > /tmp/pico-a3-doctor.json
uv run python -c 'import json; payload=json.load(open("/tmp/pico-a3-doctor.json")); security=payload["data"]["security"]; assert security["status"] in {"ok","review_required"}; assert "project_env" in security and "private_storage" in security and "recovery_review" in security; print("doctor security contract valid")'
```

Expected: focused tests pass; help scan prints all three concepts; the final command prints `doctor security contract valid`; no command output contains a secret value.

- [ ] **Step 7: Run Ruff and commit**

Run: `uv run ruff check pico/cli_recovery.py pico/cli_diagnostics.py pico/cli_commands.py tests/test_cli_commands.py tests/test_cli_diagnostics.py tests/test_recovery_cli.py`

Expected: no diagnostics.

```bash
git add pico/cli_recovery.py pico/cli_diagnostics.py pico/cli_commands.py README.md tests/test_cli_commands.py tests/test_cli_diagnostics.py tests/test_recovery_cli.py
git commit -m "docs(security): document local trust boundaries"
```

---

### Task 5: Add Offline Security Assertions to the Existing Live Harness

**Files:**
- Modify: `benchmarks/live_e2e/run_live_session.py`
- Modify: `benchmarks/live_e2e/tests/test_assertions.py`
- Modify: `benchmarks/live_e2e/README.md`

**Interfaces:**
- Consumes: existing five-turn `RunConfig`, `TurnRunner`, `AssertionEngine`, `Reporter`, and `_SniffingProviderWrapper`.
- Consumes: A1 `redact_artifact()`, `SensitiveDataBlockedError`, private-file helpers, and exact project env loading.
- Produces: three additional global assertions named `provider_payloads_exclude_api_key`, `active_artifacts_exclude_api_key`, and `active_private_artifact_modes`; the passing live contract becomes exactly 43/43 assertions.
- Changes `Reporter.write_json()` by adding keyword-only `artifact_security`, `redactor`, and `forbidden_values`; it writes only the fully redacted payload plus a safe `artifact_security` field.

- [ ] **Step 1: Add failing active-artifact scanner tests**

Add to `benchmarks/live_e2e/tests/test_assertions.py`:

```python
def test_active_artifact_scan_detects_secret_and_mode_failures(
    tmp_path
):
    secret = "ghp_" + "A" * 32
    pico_root = tmp_path / ".pico"
    before = run_live_session.snapshot_private_artifacts(pico_root)
    run_dir = pico_root / "runs" / "run-test"
    run_dir.mkdir(parents=True)
    artifact = run_dir / "trace.jsonl"
    artifact.write_text(secret, encoding="utf-8")
    artifact.chmod(0o644)
    if os.name == "posix":
        for directory in (pico_root, pico_root / "runs", run_dir):
            directory.chmod(0o700)

    result = run_live_session.scan_active_private_artifacts(
        pico_root,
        before,
        forbidden_values=(secret,),
    )

    assert result["files_scanned"] == 1
    assert result["secret_hits"] == [
        ".pico/runs/run-test/trace.jsonl"
    ]
    if os.name == "posix":
        assert result["mode_failures"] == [
            ".pico/runs/run-test/trace.jsonl:0644"
        ]


def test_active_artifact_scan_ignores_unchanged_baseline_file(
    tmp_path
):
    pico_root = tmp_path / ".pico"
    session = pico_root / "sessions" / "old.json"
    session.parent.mkdir(parents=True)
    session.write_text("old", encoding="utf-8")
    if os.name == "posix":
        pico_root.chmod(0o700)
        session.parent.chmod(0o700)
        session.chmod(0o600)
    before = run_live_session.snapshot_private_artifacts(pico_root)

    result = run_live_session.scan_active_private_artifacts(
        pico_root,
        before,
        forbidden_values=("old",),
    )

    assert result == {
        "files_scanned": 0,
        "secret_hits": [],
        "mode_failures": [],
    }
```

- [ ] **Step 2: Run scanner tests and confirm red**

Run: `uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q`

Expected: FAIL because the snapshot and scan helpers do not exist. The offline test must not construct a Provider client.

- [ ] **Step 3: Implement safe baseline and delta scanning**

Add to `benchmarks/live_e2e/run_live_session.py`:

```python
import os
import stat


def _private_tree_entries(pico_root):
    try:
        root_info = pico_root.lstat()
    except FileNotFoundError:
        return []
    entries = [(pico_root, root_info)]
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(
        root_info.st_mode
    ):
        return entries
    for current, dirnames, filenames in os.walk(
        pico_root,
        followlinks=False,
    ):
        dirnames.sort()
        base = Path(current)
        for name in sorted(dirnames + filenames):
            path = base / name
            try:
                entries.append((path, path.lstat()))
            except OSError:
                entries.append((path, None))
    return entries


def snapshot_private_artifacts(pico_root):
    pico_root = Path(pico_root)
    snapshot = {}
    for path, info in _private_tree_entries(pico_root):
        if info is not None and stat.S_ISREG(info.st_mode):
            snapshot[path.relative_to(pico_root).as_posix()] = (
                info.st_ctime_ns,
                info.st_mtime_ns,
                info.st_size,
            )
    return snapshot


def scan_active_private_artifacts(
    pico_root,
    before,
    *,
    forbidden_values,
):
    pico_root = Path(pico_root)
    forbidden = tuple(
        str(value).encode()
        for value in forbidden_values
        if str(value)
    )
    secret_hits = []
    mode_failures = []
    files_scanned = 0
    for path, info in _private_tree_entries(pico_root):
        relative = path.relative_to(pico_root).as_posix()
        display = ".pico" if relative == "." else ".pico/" + relative
        if info is None:
            mode_failures.append(display + ":unreadable")
            continue
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
        ):
            mode_failures.append(display + ":unsafe-type")
            continue
        if stat.S_ISDIR(info.st_mode):
            if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o700:
                mode_failures.append(
                    display + ":" + format(stat.S_IMODE(info.st_mode), "04o")
                )
            continue
        if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600:
            mode_failures.append(
                display + ":" + format(stat.S_IMODE(info.st_mode), "04o")
            )
        marker = (info.st_ctime_ns, info.st_mtime_ns, info.st_size)
        if before.get(relative) == marker:
            continue
        files_scanned += 1
        body = path.read_bytes()
        if any(value in body for value in forbidden):
            secret_hits.append(display)
    return {
        "files_scanned": files_scanned,
        "secret_hits": secret_hits,
        "mode_failures": mode_failures,
    }
```

The scanner returns only safe relative paths and modes. It never returns forbidden values, hashes, lengths, environment names, request headers, or URLs.

- [ ] **Step 4: Add Provider-payload booleans and three global assertions**

In `_SniffingProviderWrapper.__init__`, accept `forbidden_values=()` and store only the tuple privately. In `complete_v2()`, append:

```python
serialized = json.dumps(
    {"system": system, "tools": tools, "messages": messages},
    ensure_ascii=False,
)
payload_secret_clean = all(
    not value or value not in serialized
    for value in self._forbidden_values
)
self.calls.append(
    {
        "last_user_content": last_user,
        "call_ts_ns": time.monotonic_ns(),
        "payload_secret_clean": payload_secret_clean,
    }
)
if not payload_secret_clean:
    raise SensitiveDataBlockedError("live provider payload contains blocked sensitive material")
return self._inner.complete_v2(
    system=system,
    tools=tools,
    messages=messages,
    max_tokens=max_tokens,
    cache_breakpoints=cache_breakpoints,
)
```

Remove the previous append so each attempted Provider call still has exactly one capture. The check occurs before delegating, so `payload_secret_clean=False` produces zero real Provider calls. `make_live_client()` passes only the selected API key as a forbidden value.

Change `AssertionEngine.check_global()` to accept `artifact_security`. Append exactly:

```python
provider_clean = bool(pico.model_client.calls) and all(
    call.get("payload_secret_clean") is True
    for call in pico.model_client.calls
)
out.append(
    Assertion(
        name="provider_payloads_exclude_api_key",
        passed=provider_clean,
        expected="every captured Provider payload excludes the selected API key",
        actual=str(provider_clean),
    )
)
artifact_clean = not artifact_security["secret_hits"]
out.append(
    Assertion(
        name="active_artifacts_exclude_api_key",
        passed=artifact_clean,
        expected="new or changed .pico artifacts contain no selected API key",
        actual=str(artifact_security["secret_hits"]),
    )
)
private_modes = not artifact_security["mode_failures"]
out.append(
    Assertion(
        name="active_private_artifact_modes",
        passed=private_modes,
        expected="active private files are 0600 and directories are 0700",
        actual=str(artifact_security["mode_failures"]),
    )
)
```

In `main()`, snapshot `.pico` before fixture/runtime construction. After turns and before global assertions, scan using the selected key. Pass only the safe scan result into `check_global()` and Reporter.

Extend `Reporter.write_json()` with this final write boundary after its existing payload/summary construction:

```python
payload["artifact_security"] = {
    "files_scanned": int(artifact_security["files_scanned"]),
    "secret_hits": list(artifact_security["secret_hits"]),
    "mode_failures": list(artifact_security["mode_failures"]),
}
safe_payload = redactor(payload)
serialized = json.dumps(safe_payload, indent=2, ensure_ascii=False)
if any(str(value) and str(value) in serialized for value in forbidden_values):
    raise SensitiveDataBlockedError("live report contains blocked sensitive material")
report_path = self.output_dir / f"{run_id}.json"
ensure_private_dir(self.output_dir)
descriptor, temp_name = tempfile.mkstemp(prefix=report_path.name + ".", dir=self.output_dir)
temp_path = Path(temp_name)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, report_path)
    ensure_private_file(report_path)
    directory_fd = os.open(self.output_dir, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if temp_path.exists():
        temp_path.unlink()
return report_path
```

`main()` constructs `redactor = lambda value: redact_artifact(value, env={"PICO_LIVE_API_KEY": selected_api_key})` and passes the key only through the private `forbidden_values` argument. The key, its digest, and its length never enter `payload`. This full-payload boundary covers user prompts, final text, Provider-derived assertion actuals, aborted failures, and the safe artifact summary, including reports retained after failure.

- [ ] **Step 5: Add offline assertion and report tests**

Add tests proving:

- a false `payload_secret_clean` call fails only `provider_payloads_exclude_api_key`;
- a secret hit fails only `active_artifacts_exclude_api_key`;
- a mode failure fails only `active_private_artifact_modes`;
- clean inputs add exactly three passing assertions;
- Reporter output never contains the selected key;
- Reporter output contains exact safe `artifact_security` keys and no forbidden value even when user/final/assertion text contains the selected key;
- a wrapper payload leak raises before the delegate Provider spy and leaves its call count at zero;
- mocked offline `main()` never enters `make_live_client()` when preflight fails.

Update every existing `check_global()` call with a clean artifact payload:

```python
clean_artifacts = {
    "files_scanned": 3,
    "secret_hits": [],
    "mode_failures": [],
}
```

- [ ] **Step 6: Update the live harness README**

Document that:

- the existing five turns and 15-call/200000-token caps remain unchanged;
- the selected Provider is DeepSeek or Anthropic per invocation, but A3's final authorized run is DeepSeek only;
- deterministic tests are the security source of truth;
- the live gate checks Provider payloads and only new/changed `.pico` artifacts;
- `.env` is intentionally outside artifact scanning;
- the report stores only safe paths/counts and never the key;
- this remains a local harness without an OS sandbox.

- [ ] **Step 7: Run offline harness gates and commit**

```bash
uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q
uv run ruff check benchmarks/live_e2e/run_live_session.py benchmarks/live_e2e/tests/test_assertions.py
uv run python -m py_compile benchmarks/live_e2e/run_live_session.py
```

Expected: all offline tests pass; Ruff and py_compile are silent; no network request occurs.

```bash
git add benchmarks/live_e2e/run_live_session.py benchmarks/live_e2e/tests/test_assertions.py benchmarks/live_e2e/README.md
git commit -m "test(e2e): verify provider and artifact secret boundaries"
```

---

### Task 6: Pass the Full Local Gate and Generate Fresh Deterministic Evidence

**Files:**
- Create: `benchmarks/results/security-trust-baseline-2026-07-10/harness-regression-v2.json`
- Create: `benchmarks/results/security-trust-baseline-2026-07-10/context-ablation-v2.json`
- Create: `benchmarks/results/security-trust-baseline-2026-07-10/memory-ablation-v2.json`
- Create: `benchmarks/results/security-trust-baseline-2026-07-10/recovery-ablation-v2.json`
- Create: `benchmarks/results/security-trust-baseline-2026-07-10/memory-quality.json`
- Create: `benchmarks/results/security-trust-baseline-2026-07-10/pico-benchmark-core-report.md`
- Create: `benchmarks/results/security-trust-baseline-2026-07-10/DATA_PROVENANCE.md`
- Modify: `docs/review-pack/README.md`
- Modify: `docs/review-pack/dashboard.md`

**Interfaces:**
- Consumes: committed Tasks 1–5 plus the existing fixed benchmark, context/memory/recovery ablation, memory-quality, core-report, and perf modules.
- Produces: committed deterministic artifacts and review-pack links. Security and restore guarantees remain attributed to adversarial tests, not to the older recovery ablation.

- [ ] **Step 1: Check tracked-tree hygiene and structural boundaries**

```bash
git diff --check
git status --short
test ! -e pico/model_output_parser.py
! rg -n '^    def tool_(list_files|read_file|search|run_shell|write_file|patch_file|delegate)\(' pico/runtime.py
! rg -n "subprocess\\.run\\(\\s*\\[['\\\"](?:git|rg)['\\\"]" pico/workspace.py pico/workspace_observer.py pico/tool_executor.py pico/tools.py
```

Expected: `git diff --check` is silent; no uncommitted tracked implementation file; deletion checks exit 0; production WorkspaceContext/Observer/ToolExecutor/tool-search code contains no bare Git/rg subprocess call.

- [ ] **Step 2: Run the complete quality gate**

Run: `./scripts/check.sh`

Expected: exit 0; Ruff emits no diagnostics; pytest reports no failed or error test and no security/recovery skip introduced by A3.

- [ ] **Step 3: Run deterministic memory quality**

```bash
EVIDENCE=benchmarks/results/security-trust-baseline-2026-07-10
mkdir -p "$EVIDENCE"
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json > "$EVIDENCE/memory-quality.json"
```

Expected: JSON summary is exactly `{"total": 8, "passed": 8, "failed": 0, "pass_rate": 1.0}`.

- [ ] **Step 4: Generate the fresh fixed harness artifact**

```bash
uv run python -c 'from pico.evaluation.fixed_benchmark import run_harness_regression_v2; run_harness_regression_v2(artifact_path="benchmarks/results/security-trust-baseline-2026-07-10/harness-regression-v2.json")'
```

Expected: artifact summary has zero failed tasks and every row satisfies the current messages-v3 invariant.

- [ ] **Step 5: Generate context, memory, and recovery artifacts**

```bash
uv run python -c 'from pico.evaluation.metrics import run_context_ablation_v2; run_context_ablation_v2("benchmarks/results/security-trust-baseline-2026-07-10/context-ablation-v2.json", repetitions=5)'
uv run python -c 'from pico.evaluation.metrics import run_memory_ablation_v2; run_memory_ablation_v2("benchmarks/results/security-trust-baseline-2026-07-10/memory-ablation-v2.json", repetitions=5)'
uv run python -c 'from pico.evaluation.metrics import run_recovery_ablation_v2; run_recovery_ablation_v2("benchmarks/results/security-trust-baseline-2026-07-10/recovery-ablation-v2.json", repetitions=3)'
```

Expected:

- context current-request preservation is 1.0 and bounded request chars are lower than unbounded request chars;
- every memory variant records `bootstrap_tool_turn_dropped=true`;
- memory-on beats memory-off on repeated reads and memory hit rate;
- all memory correct rates are 1.0;
- recovery resume false-accept rate is 0.0.

- [ ] **Step 6: Generate the core report from only fresh artifacts**

```bash
uv run python -c 'from pico.evaluation.metrics import write_benchmark_core_report; write_benchmark_core_report(report_path="benchmarks/results/security-trust-baseline-2026-07-10/pico-benchmark-core-report.md", harness_artifact_path="benchmarks/results/security-trust-baseline-2026-07-10/harness-regression-v2.json", context_artifact_path="benchmarks/results/security-trust-baseline-2026-07-10/context-ablation-v2.json", memory_artifact_path="benchmarks/results/security-trust-baseline-2026-07-10/memory-ablation-v2.json", recovery_artifact_path="benchmarks/results/security-trust-baseline-2026-07-10/recovery-ablation-v2.json")'
```

Expected: report generation exits 0 and the report describes bounded/unbounded sent-message metrics rather than legacy prompt/history metrics.

- [ ] **Step 7: Validate all deterministic semantics**

```bash
uv run python -c 'import json,pathlib; root=pathlib.Path("benchmarks/results/security-trust-baseline-2026-07-10"); harness=json.loads((root/"harness-regression-v2.json").read_text()); context=json.loads((root/"context-ablation-v2.json").read_text()); memory=json.loads((root/"memory-ablation-v2.json").read_text()); recovery=json.loads((root/"recovery-ablation-v2.json").read_text()); quality=json.loads((root/"memory-quality.json").read_text()); assert harness["summary"]["failed"]==0; assert context["summary"]["current_request_preserved_rate"]==1.0; assert context["summary"]["avg_bounded_request_chars"]<context["summary"]["avg_unbounded_request_chars"]; variants=memory["variants"]; assert all(item["bootstrap_tool_turn_dropped"] for item in variants.values()); assert variants["memory_on"]["repeated_reads"]<variants["memory_off"]["repeated_reads"] and variants["memory_on"]["repeated_reads"]<variants["memory_irrelevant"]["repeated_reads"]; assert variants["memory_on"]["memory_hit_rate"]>variants["memory_off"]["memory_hit_rate"] and variants["memory_on"]["memory_hit_rate"]>variants["memory_irrelevant"]["memory_hit_rate"]; assert all(item["correct_rate"]==1.0 for item in variants.values()); assert recovery["variants"]["resume_enabled"]["summary"]["resume_false_accept_rate"]==0.0; assert quality["summary"]=={"total":8,"passed":8,"failed":0,"pass_rate":1.0}; print("A deterministic evidence valid")'
```

Expected: prints `A deterministic evidence valid`.

- [ ] **Step 8: Run all four perf JSON smokes**

```bash
uv run python -m benchmarks.perf.bench_build_v2 > /tmp/pico-a3-build-v2.json
uv run python -m benchmarks.perf.bench_retrieval > /tmp/pico-a3-retrieval.json
uv run python -m benchmarks.perf.bench_recall > /tmp/pico-a3-recall.json
uv run python -m benchmarks.perf.bench_security_recovery > /tmp/pico-a3-security-recovery.json
uv run python -c 'import json; paths=["/tmp/pico-a3-build-v2.json","/tmp/pico-a3-retrieval.json","/tmp/pico-a3-recall.json","/tmp/pico-a3-security-recovery.json"]; expected=[3,3,4,4]; payloads=[json.load(open(path)) for path in paths]; assert [len(payload["scenarios"]) for payload in payloads]==expected; assert all(item["min_ns"]>0 for payload in payloads for item in payload["scenarios"]); print("3/3/4/4 perf scenarios valid")'
```

Expected: prints `3/3/4/4 perf scenarios valid`. Do not add latency thresholds.

- [ ] **Step 9: Re-run the offline live harness**

Run: `uv run pytest benchmarks/live_e2e/tests/test_assertions.py -q`

Expected: all pass with no network request.

- [ ] **Step 10: Write exact provenance**

Create `benchmarks/results/security-trust-baseline-2026-07-10/DATA_PROVENANCE.md` with:

```markdown
# Data Provenance

This directory is the Pico A-stage security and trust baseline deterministic evidence set.

Authoritative generators:

- `uv run python -c 'from pico.evaluation.fixed_benchmark import run_harness_regression_v2; run_harness_regression_v2(artifact_path="benchmarks/results/security-trust-baseline-2026-07-10/harness-regression-v2.json")'`
- `uv run python -c 'from pico.evaluation.metrics import run_context_ablation_v2; run_context_ablation_v2("benchmarks/results/security-trust-baseline-2026-07-10/context-ablation-v2.json", repetitions=5)'`
- `uv run python -c 'from pico.evaluation.metrics import run_memory_ablation_v2; run_memory_ablation_v2("benchmarks/results/security-trust-baseline-2026-07-10/memory-ablation-v2.json", repetitions=5)'`
- `uv run python -c 'from pico.evaluation.metrics import run_recovery_ablation_v2; run_recovery_ablation_v2("benchmarks/results/security-trust-baseline-2026-07-10/recovery-ablation-v2.json", repetitions=3)'`
- `uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json`

Interpretation boundaries:

- Artifact canary, Shell bypass, restore durability, crash reconciliation, pending review, and private-mode claims come from deterministic adversarial pytest gates.
- Harness regression proves deterministic runtime behavior, not live Provider answer quality.
- Recovery ablation remains a resume-regression measure; it does not replace the A2 restore-journal tests.
- Performance files are local parseable smokes without machine-specific thresholds and are not committed here.
- One real DeepSeek E2E is a separate final integration gate and its JSON remains ignored.
```

- [ ] **Step 11: Update the review pack before the paid gate**

Update `docs/review-pack/README.md` to link all seven fresh files and state that local security truth comes from the named pytest gates.

Replace the A-stage rows in `docs/review-pack/dashboard.md` with:

```markdown
| ID | Status | Acceptance | Evidence |
| --- | --- | --- | --- |
| A-01 Sensitive data | Done | Provider/session/artifact/CLI canary clean | security integration tests |
| A-02 Safe execution | Done | zero automatic bypass in fixed shell corpus | shell security corpus |
| A-03 Recovery integrity | Done | durable intent, reconciliation, review, quarantine | A2 and durability E2E tests |
| A-04 Local evidence | Done | full check, deterministic benchmarks, perf smokes | current A evidence directory |
| A-05 Real E2E | Pending final gate | one DeepSeek native-tool run with key/artifact checks | ignored local report |
```

- [ ] **Step 12: Verify evidence and commit**

```bash
! rg -n 'avg_full_prompt_chars|avg_raw_prompt_chars|initial_history_empty|session\["history"\]' benchmarks/results/security-trust-baseline-2026-07-10 docs/review-pack
! rg -n 'last_prompt_metadata|prompt_metadata|prompt_cache_key' benchmarks/results/security-trust-baseline-2026-07-10 docs/review-pack
git diff --check
```

Expected: forbidden-term scans are silent and `git diff --check` exits 0.

```bash
git add benchmarks/results/security-trust-baseline-2026-07-10 docs/review-pack/README.md docs/review-pack/dashboard.md
git commit -m "docs(evidence): add security trust baseline pack"
```

---

### Task 7: Complete Independent Whole-Branch Review and Record the Local Ledger

**Files:**
- Modify on finding only: the exact A1, A2, or A3 owner named by the finding
- Modify: `.superpowers/sdd/progress.md`

**Interfaces:**
- Consumes: the committed A1, A2, and A3 Task 1–6 range plus the authoritative design's twelve Definition-of-Done items.
- Produces: an independent no-Critical/no-Important verdict and a durable local-review ledger. It does not authorize a Provider call until every repair is committed and re-reviewed.

- [ ] **Step 1: Capture the review range and clean-tree evidence**

```bash
git diff --check
git status --short
git log --oneline --decorate -20
```

Expected: no uncommitted tracked implementation/evidence file and a visible contiguous A-stage commit range.

- [ ] **Step 2: Invoke the required review skill and independent reviewer**

Use `superpowers:requesting-code-review`. Give the reviewer the A-stage baseline commit, current HEAD, design spec, A1/A2/A3 plans, and this mandatory checklist:

1. all C §11.2 commitments have implementation and tests;
2. no public Pico raw tool runner bypasses ToolExecutor;
3. normal Provider/session/artifact/approval/verification/CLI observations are canary-clean;
4. only a private 0600 migration backup may retain exact historical session bytes;
5. sensitive path/content creates no automatic restore blob;
6. fixed Shell corpus has zero automatic bypass and simple approved commands use `argv`;
7. turn checkpoint coalescing preserves exists/hash/mode continuity;
8. every restore has durable intents before mutation and crash reconciliation afterward;
9. pending/applying/partial/invalid mutation evidence enters preview-first Recovery Review;
10. `.env` has no argv secret path, is exact-root/atomic/0600, and cannot control executable resolution;
11. README/help/doctor state the human shell escape hatch and no-sandbox boundary truthfully;
12. no new dependency, Provider matrix, sandbox claim, policy framework, or latency threshold was introduced.

Expected reviewer verdict: no Critical or Important finding.

- [ ] **Step 3: Resolve every actionable finding with focused red-green evidence**

For each Critical or Important finding, add one focused failing test in the existing owning suite, run it to observe the intended failure, apply the minimum owner-boundary repair, rerun the focused suite, and create one `fix(review):` commit. Do not weaken a test, sanitizer, hash/mode check, durability step, review gate, or live assertion to obtain green.

Expected: every actionable finding is either fixed and committed or technically rejected with file/line evidence accepted by the independent reviewer.

- [ ] **Step 4: Re-run all final local gates after review repairs**

```bash
./scripts/check.sh
uv run pytest tests/test_security_integration.py tests/test_shell_security_corpus.py tests/test_recovery_durability_e2e.py benchmarks/live_e2e/tests/test_assertions.py -q
EVIDENCE=benchmarks/results/security-trust-baseline-2026-07-10
uv run python benchmarks/memory_quality/run_benchmark.py --mode fake --format json > "$EVIDENCE/memory-quality.json"
uv run python -c 'from pico.evaluation.fixed_benchmark import run_harness_regression_v2; run_harness_regression_v2(artifact_path="benchmarks/results/security-trust-baseline-2026-07-10/harness-regression-v2.json")'
uv run python -c 'from pico.evaluation.metrics import run_context_ablation_v2; run_context_ablation_v2("benchmarks/results/security-trust-baseline-2026-07-10/context-ablation-v2.json", repetitions=5)'
uv run python -c 'from pico.evaluation.metrics import run_memory_ablation_v2; run_memory_ablation_v2("benchmarks/results/security-trust-baseline-2026-07-10/memory-ablation-v2.json", repetitions=5)'
uv run python -c 'from pico.evaluation.metrics import run_recovery_ablation_v2; run_recovery_ablation_v2("benchmarks/results/security-trust-baseline-2026-07-10/recovery-ablation-v2.json", repetitions=3)'
uv run python -c 'from pico.evaluation.metrics import write_benchmark_core_report; write_benchmark_core_report(report_path="benchmarks/results/security-trust-baseline-2026-07-10/pico-benchmark-core-report.md", harness_artifact_path="benchmarks/results/security-trust-baseline-2026-07-10/harness-regression-v2.json", context_artifact_path="benchmarks/results/security-trust-baseline-2026-07-10/context-ablation-v2.json", memory_artifact_path="benchmarks/results/security-trust-baseline-2026-07-10/memory-ablation-v2.json", recovery_artifact_path="benchmarks/results/security-trust-baseline-2026-07-10/recovery-ablation-v2.json")'
uv run python -c 'import json,pathlib; root=pathlib.Path("benchmarks/results/security-trust-baseline-2026-07-10"); harness=json.loads((root/"harness-regression-v2.json").read_text()); context=json.loads((root/"context-ablation-v2.json").read_text()); memory=json.loads((root/"memory-ablation-v2.json").read_text()); recovery=json.loads((root/"recovery-ablation-v2.json").read_text()); quality=json.loads((root/"memory-quality.json").read_text()); assert harness["summary"]["failed"]==0; assert context["summary"]["current_request_preserved_rate"]==1.0; assert context["summary"]["avg_bounded_request_chars"]<context["summary"]["avg_unbounded_request_chars"]; variants=memory["variants"]; assert all(item["bootstrap_tool_turn_dropped"] for item in variants.values()); assert variants["memory_on"]["repeated_reads"]<variants["memory_off"]["repeated_reads"]; assert variants["memory_on"]["memory_hit_rate"]>variants["memory_off"]["memory_hit_rate"]; assert all(item["correct_rate"]==1.0 for item in variants.values()); assert recovery["variants"]["resume_enabled"]["summary"]["resume_false_accept_rate"]==0.0; assert quality["summary"]=={"total":8,"passed":8,"failed":0,"pass_rate":1.0}; print("post-review deterministic evidence valid")'
uv run python -m benchmarks.perf.bench_build_v2 > /tmp/pico-a3-review-build.json
uv run python -m benchmarks.perf.bench_retrieval > /tmp/pico-a3-review-retrieval.json
uv run python -m benchmarks.perf.bench_recall > /tmp/pico-a3-review-recall.json
uv run python -m benchmarks.perf.bench_security_recovery > /tmp/pico-a3-review-security-recovery.json
uv run python -c 'import json; paths=["/tmp/pico-a3-review-build.json","/tmp/pico-a3-review-retrieval.json","/tmp/pico-a3-review-recall.json","/tmp/pico-a3-review-security-recovery.json"]; assert [len(json.load(open(path))["scenarios"]) for path in paths]==[3,3,4,4]; print("post-review local gates valid")'
```

Expected: all commands exit 0; deterministic generation prints `post-review deterministic evidence valid`; the final command prints `post-review local gates valid`. Using `apply_patch`, update `DATA_PROVENANCE.md` with the exact `git rev-parse HEAD` used for this regeneration before committing it.

- [ ] **Step 5: Obtain clean independent re-review**

Send the repair commits and fresh verification evidence to the same reviewer. Require an explicit final verdict with the twelve checklist results.

Expected: `Ready` or equivalent, with zero Critical and zero Important finding.

- [ ] **Step 6: Append the local-review ledger**

Append a new `## Security and Trust Baseline 2026-07-10` section to `.superpowers/sdd/progress.md`. Record only safe facts:

- A1, A2, and A3 plan paths;
- baseline and reviewed HEAD commit IDs;
- each A3 task commit;
- focused/full test result summaries;
- deterministic evidence directory;
- 3/3/4/4 perf scenario result;
- reviewer verdict and any repaired finding;
- `Real DeepSeek E2E: pending exactly one authorized process`.

Use the measured values from Step 4. Do not record a key, environment dump, header, credential-bearing URL, or raw invalid evidence.

- [ ] **Step 7: Commit the review ledger**

```bash
git add benchmarks/results/security-trust-baseline-2026-07-10 docs/review-pack/README.md docs/review-pack/dashboard.md .superpowers/sdd/progress.md
git commit -m "chore(sdd-ledger): record A-stage local review"
git diff --check
git status --short
```

Expected: commit succeeds; `git diff --check` is silent; no uncommitted tracked file remains.

---

### Task 8: Run Exactly One Real DeepSeek E2E and Record Final Evidence

**Files:**
- Generated and ignored: `benchmarks/live_e2e/results/live-e2e-*.json`
- Modify after a passing run: `docs/review-pack/README.md`
- Modify after a passing run: `docs/review-pack/dashboard.md`
- Modify after a passing run: `.superpowers/sdd/progress.md`

**Interfaces:**
- Consumes: one configured `PICO_DEEPSEEK_API_KEY`, the committed and independently reviewed Task 7 HEAD, and the existing five-turn live harness with exactly 43 assertions.
- Produces: one ignored passing DeepSeek report, safe review-pack facts, and the final A-stage ledger. It does not run Anthropic and does not retry DeepSeek automatically.

- [ ] **Step 1: Verify the DeepSeek precondition without printing the key**

```bash
uv run python -c 'from pathlib import Path; import os; from pico.config import load_project_env; load_project_env(Path.cwd()); assert os.environ.get("PICO_DEEPSEEK_API_KEY","").strip(), "PICO_DEEPSEEK_API_KEY is not configured"; print("deepseek key configured")'
git diff --check
git status --short
```

Expected: prints only `deepseek key configured`; tracked worktree is clean; no credential is printed.

- [ ] **Step 2: Run the one authorized real process**

Run this block once:

```bash
set -e
BEFORE=$(find benchmarks/live_e2e/results -name 'live-e2e-*.json' -type f | wc -l | tr -d ' ')
uv run python -m benchmarks.live_e2e.run_live_session --provider deepseek
AFTER=$(find benchmarks/live_e2e/results -name 'live-e2e-*.json' -type f | wc -l | tr -d ' ')
test "$AFTER" -eq "$((BEFORE + 1))"
```

Expected: exit 0; terminal output contains `[live-e2e] OVERALL: ALL PASS · 43/43 assertions`; all five turns have terminal artifacts; at least one native `read_file` action is observed; Provider calls and input/output tokens remain under existing caps.

If this process fails, retain the ignored report, make no completion claim, and do not automatically rerun this block. Diagnose with offline artifacts and return the task as incomplete.

- [ ] **Step 3: Validate the newest ignored report without exposing credentials**

```bash
REPORT=$(ls -t benchmarks/live_e2e/results/live-e2e-*.json | head -1)
uv run python -c 'import json,os,pathlib,sys; from pico.config import load_project_env; report=pathlib.Path(sys.argv[1]); payload=json.loads(report.read_text(encoding="utf-8")); load_project_env(pathlib.Path.cwd()); text=report.read_text(encoding="utf-8"); assertions={item["name"]:item["passed"] for item in payload["global_assertions"]}; assert payload["overall_pass"] is True; assert payload["aborted_reason"]==""; assert payload["session_schema"]==3; assert payload["assertion_summary"]=={"total":43,"passed":43,"failed":0}; assert payload["action_origin_summary"].get("native_tool_use",0)>=1; assert payload["totals"]["provider_calls"]<=payload["config"]["max_provider_calls"]; assert payload["totals"]["input_tokens"]+payload["totals"]["output_tokens"]<=payload["config"]["max_total_tokens"]; assert assertions["provider_payloads_exclude_api_key"] is True; assert assertions["active_artifacts_exclude_api_key"] is True; assert assertions["active_private_artifact_modes"] is True; names=("PICO_DEEPSEEK_API_KEY","PICO_ANTHROPIC_API_KEY"); assert all(not os.environ.get(name) or os.environ[name] not in text for name in names); security=payload["artifact_security"]; assert security["secret_hits"]==[] and security["mode_failures"]==[]; print(report)' "$REPORT"
```

Expected: prints only the ignored report path.

- [ ] **Step 4: Verify fixture restoration and repository hygiene**

```bash
REPORT=$(ls -t benchmarks/live_e2e/results/live-e2e-*.json | head -1)
test ! -e benchmarks/live_e2e/results/pre-run-pico.toml.bak
git diff --check
git status --short
git check-ignore "$REPORT"
```

Expected: no fixture backup; no tracked live report; `git check-ignore` prints the report path; tracked worktree remains clean.

- [ ] **Step 5: Promote only safe real-gate facts in review docs**

In `docs/review-pack/dashboard.md`, replace the A-05 row with:

```markdown
| A-05 Real E2E | Done | one DeepSeek native-tool run with key/artifact checks | ignored local report; safe facts recorded in review pack |
```

In `docs/review-pack/README.md`, add the selected Provider, model name, 43/43 assertion result, native action count, Provider call count, input/output/cache-read token totals, code commit, and the fact that the JSON is intentionally ignored. Read these values from the report. Do not copy final-answer text, user content, key-related metadata, request headers, URLs, or the report itself.

- [ ] **Step 6: Append final live evidence to the ledger**

Append to the A-stage section in `.superpowers/sdd/progress.md`:

- `Real DeepSeek E2E: PASS`;
- safe model, assertion, native-action, call, token, wall-time, report-path, and code-commit facts read from the validated report;
- `automatic retries: 0`;
- final local and independent review verdicts.

Do not record API key names beyond the canonical configuration variable already documented by the CLI, and never record a value, hash, length, header, or credential-bearing URL.

- [ ] **Step 7: Run final documentation checks and commit**

```bash
uv run pytest tests/test_cli_commands.py tests/test_cli_diagnostics.py tests/test_recovery_cli.py benchmarks/live_e2e/tests/test_assertions.py -q
git diff --check
git add docs/review-pack/README.md docs/review-pack/dashboard.md .superpowers/sdd/progress.md
git commit -m "chore(sdd-ledger): record A-stage final verification"
git status --short
```

Expected: focused tests pass; commit succeeds; no uncommitted tracked file; ignored live JSON remains local.

Do not run another Provider process, publish, push, open a PR, or start a later phase.

---

## Requirement-to-Task Traceability

| A-stage requirement | Owning A3 task |
| --- | --- |
| Whole-chain redaction and Provider boundary | 1, 5 |
| Sensitive file/snapshot and trusted execution integration | 1, 2 |
| Shell fail-closed corpus and approval metadata | 2 |
| A→B→C, hash/mode, journal, crash, and partial restore | 3 |
| Pending/applying/partial/invalid Recovery Review | 3, 4 |
| `.env`, private modes, help, doctor, and no-sandbox truth | 1, 4 |
| Full check, deterministic benchmark, memory quality, perf | 6 |
| Independent review and auditable local ledger | 7 |
| Exactly one real DeepSeek E2E | 8 |

## Plan Self-Review Gate

Run these checks before A3 implementation begins:

```bash
PLAN=docs/superpowers/plans/2026-07-10-pico-a3-integration-evidence-live-e2e.md
! rg -n 'TO[D]O|TB[D]|FIXM[E]|NotImplementedErro[r]|\.\.\.|sa[m]e as|同[上]|simila[r] to' "$PLAN"
test "$(rg -c '^### Task ' "$PLAN")" -eq 8
test "$(rg -c '^\*\*Interfaces:\*\*' "$PLAN")" -eq 8
test "$(rg -c 'git commit -m ' "$PLAN")" -ge 8
test "$(rg -c 'run_live_sessio[n] --provider deepseek' "$PLAN")" -eq 1
rg -n '^### Task ' "$PLAN"
rg -n 'discover_lexical_repo_root|build_trusted_executables|run_hardened_git|run_hardened_rg|project_env_path|read_project_env|load_project_env|write_project_env_assignments|sanitize_provider_payload|assess_command|pending_recovery_reviews|preview_restore_journal_resolution|resolve_restore_journal' "$PLAN"
! rg -n 'find_lexical_repo_boundar[y]|build_trusted_executable_ma[p]' "$PLAN"
```

Expected:

- placeholder scan is silent;
- exactly eight task headings appear in numeric order from Task 1 through Task 8;
- exactly eight `Interfaces` blocks exist;
- every task contains a commit step;
- only the frozen A1/A2 interface spellings appear;
- no Provider command appears before Task 8;
- Task 8 contains one DeepSeek live-module invocation and an explicit no-automatic-retry rule.

## Execution Handoff

After A1 and A2 are complete and this plan passes its self-review gate, execute with one method:

1. **Subagent-Driven:** use `superpowers:subagent-driven-development`, dispatch one fresh worker per task, and review between tasks.
2. **Inline:** use `superpowers:executing-plans`, run tasks sequentially, and honor every local gate before Task 8.

Do not mix execution methods in one run.
