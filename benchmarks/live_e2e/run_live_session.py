"""Pony live-provider end-to-end harness.

One invocation uses the Provider selected by the target repository's ``.env``
and records trace-backed evidence for five designed turns. This standalone
command consumes API credits; incomplete or malformed trace usage fails the
transport-cost pass condition and is reported as degraded instead of falling
back to mutable provider state. Reports omit provider configuration secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pony.config.environment import read_project_env
from pony.config.model import (
    API_KEY_ENV_NAME,
    resolve_model_config,
)
from pony.providers.probe import resolve_provider_client
from benchmarks.evaluation.metrics_common import (
    _decode_json_object,
    _load_json_artifact,
)
from pony.agent.messages import MessageValidationError, validate_messages
from pony.security.private_files import (
    ensure_private_dir,
    ensure_private_file,
    private_directory_identity,
    write_private_bytes_atomic,
)
from pony.security.redaction import (
    SensitiveDataBlockedError,
    contains_secret_material,
    redact_artifact,
)
from pony.state.session_store import (
    SESSION_FORMAT_VERSION,
    SESSION_HEADER_RECORD_TYPE,
    SESSION_RECORD_TYPE,
)
from pony.runtime.options import RuntimeOptions


LIVE_E2E_REPORT_FORMAT_VERSION = 3
_PROTOCOLS_BY_PROVIDER = {
    "anthropic": frozenset({"anthropic_messages"}),
    "ollama": frozenset({"ollama_chat"}),
    "openai": frozenset({"openai_chat_completions", "openai_responses"}),
}
_RESOLUTION_SOURCES = frozenset(
    {"explicit", "known_origin", "session_binding", "probe"}
)


def _positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def load_live_report(path):
    return _load_json_artifact(
        path,
        "live_e2e_report",
        LIVE_E2E_REPORT_FORMAT_VERSION,
    )


def _private_tree_entries(pony_root):
    try:
        root_info = pony_root.lstat()
    except FileNotFoundError:
        return []
    entries = [(pony_root, root_info)]
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return entries
    for current, dirnames, filenames in os.walk(
        pony_root,
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


def snapshot_private_artifacts(pony_root):
    pony_root = Path(pony_root)
    snapshot = {}
    for path, info in _private_tree_entries(pony_root):
        if info is not None and stat.S_ISREG(info.st_mode):
            snapshot[path.relative_to(pony_root).as_posix()] = (
                info.st_ctime_ns,
                info.st_mtime_ns,
                info.st_size,
            )
    return snapshot


def scan_active_private_artifacts(pony_root, before, *, forbidden_values):
    pony_root = Path(pony_root)
    forbidden = tuple(str(value).encode() for value in forbidden_values if str(value))
    secret_hits = []
    mode_failures = []
    files_scanned = 0
    for path, info in _private_tree_entries(pony_root):
        relative = path.relative_to(pony_root).as_posix()
        display = ".pony" if relative == "." else ".pony/" + relative
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


@dataclass(frozen=True)
class RunConfig:
    """CLI + env-derived configuration for one live-e2e run."""

    repo_root: Path
    provider: Literal["anthropic", "auto", "ollama", "openai"]
    model: str
    max_model_attempts: int
    max_total_tokens: int
    request_timeout_seconds: int
    max_wall_seconds: int
    reset: bool
    verbose: bool


def _provider_settings_from_config(resolved, *, required):
    if required and resolved["resolution_status"] != "resolved":
        raise ValueError("provider_detection_failed")
    protocol = resolved["protocol"]["value"]
    if protocol.startswith("openai_"):
        provider = "openai"
    elif protocol:
        provider = protocol.split("_", 1)[0]
    else:
        provider = resolved["provider"]["value"]
    return {
        "provider": provider,
        "model": resolved["model"]["value"],
        "base_url": resolved["base_url"]["value"],
        "api_key": resolved["api_key"]["value"],
        "api_key_env": API_KEY_ENV_NAME,
        "transport": protocol,
        "auth_mode": resolved["auth_mode"]["value"],
        "capabilities": dict(resolved["capabilities"]),
    }


def provider_settings(
    repo_root=None,
    *,
    project_env=None,
    process_env=None,
    required=True,
):
    root = Path.cwd() if repo_root is None else Path(repo_root)
    project_values = (
        read_project_env(root) if project_env is None else dict(project_env)
    )
    resolved = resolve_model_config(
        project_env=project_values,
        process_env=dict(os.environ if process_env is None else process_env),
        required=required,
    )
    return _provider_settings_from_config(resolved, required=required)


def resolve_live_provider(config, *, project_env, process_env):
    """Resolve one exact production client before sending the live workload."""
    unresolved = resolve_model_config(
        project_env=project_env,
        process_env=process_env,
    )
    client, resolved, report = resolve_provider_client(
        unresolved,
        timeout=config.request_timeout_seconds,
    )
    return client, _provider_settings_from_config(resolved, required=True), report


def parse_args(argv=None, *, project_env=None, process_env=None) -> RunConfig:
    """Parse arguments and resolve the target repository's ``.env``."""
    parser = argparse.ArgumentParser(prog="run_live_session")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository whose .env selects the Provider and Transport.",
    )
    parser.add_argument("--max-model-attempts", type=_positive_int, default=15)
    parser.add_argument("--max-total-tokens", type=_positive_int, default=200_000)
    parser.add_argument("--request-timeout-seconds", type=_positive_int, default=300)
    parser.add_argument("--max-wall-seconds", type=_positive_int, default=900)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    settings = provider_settings(
        repo_root,
        project_env={} if args.reset else project_env,
        process_env={} if args.reset else process_env,
        required=False,
    )
    return RunConfig(
        repo_root=repo_root,
        provider=settings["provider"],
        model=settings["model"],
        max_model_attempts=args.max_model_attempts,
        max_total_tokens=args.max_total_tokens,
        request_timeout_seconds=args.request_timeout_seconds,
        max_wall_seconds=args.max_wall_seconds,
        reset=args.reset,
        verbose=args.verbose,
    )


def check_env(config: RunConfig, *, settings=None) -> None:
    """Abort with exit 2 if the selected provider API key is missing."""
    if config.reset or config.provider == "ollama":
        return  # reset and local Ollama paths don't need an API key
    settings = settings or provider_settings(config.repo_root)
    key = settings["api_key"].strip()
    if not key:
        required_name = settings["api_key_env"]
        print(f"[live-e2e] missing {required_name}, aborted", file=sys.stderr)
        raise SystemExit(2)


def verify_pony_repo(root: Path) -> None:
    """Abort with exit 2 if ``root`` is not a pony repository."""
    runtime_entry = root / "pony" / "runtime" / "application.py"
    if not runtime_entry.is_file():
        print(
            f"[live-e2e] {root} does not look like a pony repo "
            "(missing pony/runtime/application.py), aborted",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not (root / "pyproject.toml").is_file():
        print(
            f"[live-e2e] {root}/pyproject.toml missing, aborted",
            file=sys.stderr,
        )
        raise SystemExit(2)


FIXTURE_PONY_TOML = """\
[model]
context_window = 24576
output_limit = 4096

[context]
system_tools_hard_cap = 4915
source_pool_tokens = 3072

[context.compaction]
enabled = true
reserve_tokens = 4096
keep_recent_tokens = 4096

[context.tool_results]
inline_tokens = 4096
digest_tokens = 512

[memory.recall]
min_score = 0.2
"""


SEED_NOTE_REL = Path(".pony/memory/notes/cache-invariant.md")
TOOL_DIGEST_FIXTURE_REL = Path(
    "benchmarks/live_e2e/fixtures/live_tool_digest_fixture.txt"
)
TOOL_DIGEST_FIXTURE_TEXT = "digest-fixture-token " * 5_000 + "\n"
PONY_TOML_REL = Path("pony.toml")
BACKUP_REL = Path("benchmarks/live_e2e/results/pre-run-pony.toml.bak")
COMPACTION_FIXTURE_MESSAGES = 80


def seed_compaction_fixture(pony) -> int:
    """Append enough inert history for turn four to exercise auto-compaction."""
    messages = []
    for index in range(COMPACTION_FIXTURE_MESSAGES):
        payload = " ".join(f"fixture{index:02d}token{part:03d}" for part in range(48))
        messages.append(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"compaction-fixture-{index:02d} {payload}",
                "_pony_meta": {
                    "created_at": (
                        f"2026-07-15T{index // 60:02d}:{index % 60:02d}:00+00:00"
                    ),
                    "origin": "live_e2e_compaction_fixture",
                },
            }
        )
    pony.session_store.append_messages(pony.session["id"], messages)
    pony.session["messages"].extend(messages)
    return len(messages)


class FixtureManager:
    """Install and restore the live config, Memory, and tool-result fixtures.

    On enter:
      1. If a pre-existing pony.toml is present, copy it to
         ``benchmarks/live_e2e/results/pre-run-pony.toml.bak`` so
         teardown can restore it.
      2. Write ``FIXTURE_PONY_TOML`` to ``<repo_root>/pony.toml``.
      3. Write the fixture seed note to
         ``<repo_root>/.pony/memory/notes/cache-invariant.md``.
      4. Write one large-line read fixture that deterministically triggers digest.

    On exit (never raises):
      1. Remove the seed note and digest fixture if present.
      2. Restore original pony.toml from backup, or delete the fixture
         copy if no backup existed.
    """

    def __init__(self, repo_root: Path, *, forbidden_values=()):
        self.repo_root = Path(repo_root)
        self._forbidden_values = tuple(
            str(value).encode() for value in forbidden_values if str(value)
        )
        self._seed_source = (
            Path(__file__).resolve().parent / "fixtures" / "seed_cache_note.md"
        )
        self._had_pony_toml = False
        self._original_pony_toml: bytes | None = None
        self.cleanup_errors: list[str] = []

    def __enter__(self) -> "FixtureManager":
        pony_toml = self.repo_root / PONY_TOML_REL
        backup = self.repo_root / BACKUP_REL
        digest_target = self.repo_root / TOOL_DIGEST_FIXTURE_REL
        try:
            digest_target.lstat()
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(f"live fixture already exists: {digest_target}")
        # 1. Snapshot if present
        if pony_toml.exists():
            self._had_pony_toml = True
            original = pony_toml.read_bytes()
            self._original_pony_toml = original
            contains_sensitive = contains_secret_material(
                original.decode("utf-8", errors="replace"),
                env=os.environ,
            )
            if contains_sensitive or any(
                value in original for value in self._forbidden_values
            ):
                raise SensitiveDataBlockedError(
                    "live fixture backup contains blocked sensitive material"
                )
            backup_root = ensure_private_dir(backup.parent)
            write_private_bytes_atomic(
                backup,
                original,
                trusted_root=backup_root,
                trusted_root_identity=private_directory_identity(backup_root),
            )
        try:
            # 2. Write fixture
            pony_toml.write_text(FIXTURE_PONY_TOML, encoding="utf-8")
            # 3. Write seed note
            seed_target = self.repo_root / SEED_NOTE_REL
            ensure_private_dir(seed_target.parent)
            seed_target.write_text(
                self._seed_source.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            ensure_private_file(seed_target)
            digest_target.parent.mkdir(parents=True, exist_ok=True)
            digest_target.write_text(TOOL_DIGEST_FIXTURE_TEXT, encoding="utf-8")
        except Exception:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Never raise: log-then-swallow all teardown errors.
        try:
            seed_target = self.repo_root / SEED_NOTE_REL
            if seed_target.exists():
                seed_target.unlink()
            digest_target = self.repo_root / TOOL_DIGEST_FIXTURE_REL
            if os.path.lexists(digest_target):
                digest_target.unlink()
        except OSError:
            self.cleanup_errors.append("seed_remove_failed")
            print("[live-e2e] teardown: could not remove seed note", file=sys.stderr)
        try:
            pony_toml = self.repo_root / PONY_TOML_REL
            backup = self.repo_root / BACKUP_REL
            if self._had_pony_toml:
                if not backup.exists():
                    self.cleanup_errors.append("config_backup_missing")
                else:
                    pony_toml.write_bytes(backup.read_bytes())
                    backup.unlink()
            elif pony_toml.exists():
                pony_toml.unlink()
        except OSError:
            self.cleanup_errors.append("config_restore_failed")
            print("[live-e2e] teardown: pony.toml restore failed", file=sys.stderr)

    def restoration_status(self):
        pony_toml = self.repo_root / PONY_TOML_REL
        backup = self.repo_root / BACKUP_REL
        seed = self.repo_root / SEED_NOTE_REL
        digest_target = self.repo_root / TOOL_DIGEST_FIXTURE_REL
        try:
            config_restored = (
                pony_toml.read_bytes() == self._original_pony_toml
                if self._had_pony_toml and pony_toml.is_file()
                else not self._had_pony_toml and not pony_toml.exists()
            )
            restored = (
                not self.cleanup_errors
                and not seed.exists()
                and not os.path.lexists(digest_target)
                and not backup.exists()
                and config_restored
            )
        except OSError:
            restored = False
            if "restoration_check_failed" not in self.cleanup_errors:
                self.cleanup_errors.append("restoration_check_failed")
        return {
            "restored": restored,
            "cleanup_error_codes": tuple(self.cleanup_errors),
        }


@dataclass(frozen=True)
class TurnResult:
    """Immutable record of a single turn's execution."""

    turn: int
    user_prompt: str
    expected_behavior: str
    final_answer: str
    metadata: dict
    session_message_count_before: int
    session_message_count_after: int
    model_turns_this_turn: int
    model_attempts_this_turn: int
    model_failures_this_turn: int
    transport_attempts_this_turn: int | None
    transport_retries_this_turn: int | None
    transport_evidence_complete: bool
    billing_ambiguous: bool
    duration_ms: int
    usage: dict
    stopped_at_step_limit: bool
    error_code: str | None
    provider_input_messages_len: int
    current_user_content: str
    usage_complete: bool
    request_metadata_by_call: tuple[dict, ...]
    system_prefix_hashes: tuple[str, ...]
    action_origins: tuple[str, ...]
    actual_user_contents: tuple[str, ...]
    run_id: str
    task_state_terminal: bool
    report_terminal: bool
    trace_terminal: bool
    terminal_status: str
    stop_reason: str
    tool_name_counts: dict
    tool_status_counts: dict
    error_code_counts: dict


_LIVE_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_DEGRADED_USAGE_ASSERTIONS = frozenset(
    {"turn_usage_complete", "all_turn_usage_complete"}
)
_TERMINAL_STATE_PAIRS = {
    ("completed", "final_answer_returned"),
    ("stopped", "step_limit_reached"),
    ("stopped", "retry_limit_reached"),
    ("stopped", "interrupted"),
    ("failed", "model_error"),
    ("failed", "persistence_error"),
    ("failed", "runtime_error"),
}


def _stable_trace_label(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 100
        and value.isascii()
        and all(character.isalnum() or character in "_-" for character in value)
    )


def _provider_resolution_evidence(report, provider):
    if not isinstance(report, dict):
        raise ValueError("invalid provider resolution evidence")
    report = dict(report or {})
    status = report.get("status", "not_run")
    candidate_count = report.get("candidate_count", 0)
    model_calls = report.get("model_calls", 0)
    usage_status = report.get("usage_status", "not_checked")
    resolution_source = report.get("resolution_source", "")
    protocol = report.get("protocol", "")
    if (
        status not in {"not_run", "ok"}
        or type(candidate_count) is not int
        or not 0 <= candidate_count <= 3
        or type(model_calls) is not int
        or not 0 <= model_calls <= 6
        or usage_status not in {"complete", "degraded", "not_checked"}
        or resolution_source not in _RESOLUTION_SOURCES
        or protocol not in _PROTOCOLS_BY_PROVIDER.get(provider, ())
    ):
        raise ValueError("invalid provider resolution evidence")
    if status == "not_run" and (candidate_count != 0 or model_calls != 0):
        raise ValueError("invalid provider resolution evidence")
    if status == "not_run" and usage_status != "not_checked":
        raise ValueError("invalid provider resolution evidence")
    if status == "not_run" and resolution_source == "probe":
        raise ValueError("invalid provider resolution evidence")
    if status == "ok" and (candidate_count < 1 or model_calls < 2):
        raise ValueError("invalid provider resolution evidence")
    if status == "ok" and (
        resolution_source != "probe" or usage_status == "not_checked"
    ):
        raise ValueError("invalid provider resolution evidence")
    return {
        "status": status,
        "resolution_source": resolution_source,
        "protocol": protocol,
        "candidate_count": candidate_count,
        "model_calls": model_calls,
        "usage_status": usage_status,
    }


def _exception_error_code_counts(exc):
    counts = {}
    seen = set()
    current = exc
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "code", None)
        if _stable_trace_label(code):
            counts[code] = counts.get(code, 0) + 1
        current = current.__cause__ or current.__context__
    return counts


def _empty_trace_capture():
    return {
        "model_turns": 0,
        "model_attempts": 0,
        "model_failures": 0,
        "auxiliary_model_outcomes": 0,
        "transport_attempts": None,
        "transport_retries": None,
        "transport_evidence_complete": False,
        "billing_ambiguous": True,
        "usage": {key: 0 for key in _LIVE_USAGE_KEYS},
        "usage_complete": False,
        "request_metadata": [],
        "system_prefix_hashes": [],
        "action_origins": [],
        "tool_name_counts": {},
        "tool_status_counts": {},
        "error_code_counts": {},
    }


def _read_trace_events(trace_path):
    try:
        lines = Path(trace_path).read_text(encoding="utf-8").splitlines()
        return [_decode_json_object(line) for line in lines if line.strip()]
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _trace_label_counts(events):
    tool_names = {}
    tool_statuses = {}
    error_codes = {}
    for event in events:
        if event.get("event") in {"tool_executed", "tool_interrupted"}:
            for key, counts in (("name", tool_names), ("tool_status", tool_statuses)):
                value = event.get(key)
                if _stable_trace_label(value):
                    counts[value] = counts.get(value, 0) + 1
        for key in ("reason_code", "tool_error_code", "sandbox_error_code"):
            value = event.get(key)
            if _stable_trace_label(value):
                error_codes[value] = error_codes.get(value, 0) + 1
    return tool_names, tool_statuses, error_codes


def _trace_usage(turns):
    totals = {key: 0 for key in _LIVE_USAGE_KEYS}
    usage_complete = bool(turns)
    request_metadata = []
    prefix_hashes = []
    for turn in turns:
        usage = turn.get("completion_usage")
        if not isinstance(usage, dict):
            usage_complete = False
            usage = {}
        if any(type(usage.get(key)) is not int for key in ("input_tokens", "output_tokens")):
            usage_complete = False
        for key in _LIVE_USAGE_KEYS:
            value = usage.get(key)
            if type(value) is int:
                totals[key] += value

        metadata = turn.get("request_metadata")
        if not isinstance(metadata, dict):
            usage_complete = False
            metadata = {}
        request_metadata.append(dict(metadata))
        prefix_hash = metadata.get("system_prefix_hash", "")
        prefix_hashes.append(prefix_hash if isinstance(prefix_hash, str) else "")
    return totals, usage_complete, request_metadata, prefix_hashes


def _trace_auxiliary_usage(turns):
    totals = {key: 0 for key in _LIVE_USAGE_KEYS}
    usage_complete = True
    for turn in turns:
        usage = turn.get("completion_usage")
        if not isinstance(usage, dict):
            usage_complete = False
            usage = {}
        if any(
            type(usage.get(key)) is not int
            for key in ("input_tokens", "output_tokens")
        ):
            usage_complete = False
        for key in _LIVE_USAGE_KEYS:
            value = usage.get(key)
            if type(value) is int:
                totals[key] += value
    return totals, usage_complete


def _trace_transport(requests, outcomes):
    complete = bool(requests) and len(requests) == len(outcomes) and all(
        event.get("transport_evidence_complete") is True
        and type(event.get("transport_attempts")) is int
        and type(event.get("transport_retries")) is int
        for event in outcomes
    )
    if not complete:
        return None, None, False
    return (
        sum(event["transport_attempts"] for event in outcomes),
        sum(event["transport_retries"] for event in outcomes),
        True,
    )


def read_turn_trace(trace_path):
    """Read complete per-call evidence for one persisted run trace."""
    events = _read_trace_events(trace_path)
    if events is None:
        return _empty_trace_capture()

    requests = [event for event in events if event.get("event") == "model_requested"]
    turns = [event for event in events if event.get("event") == "model_turn"]
    failures = [event for event in events if event.get("event") == "model_failed"]
    auxiliary_failures = [
        event
        for event in failures
        if event.get("attempt_origin") in {"compaction", "split_compaction"}
    ]
    auxiliary_turns = [
        event
        for event in events
        if event.get("event") == "model_auxiliary_completed"
    ]
    actions = [event for event in events if event.get("event") == "action_decoded"]
    tool_name_counts, tool_status_counts, error_code_counts = _trace_label_counts(events)
    totals, usage_complete, request_metadata, prefix_hashes = _trace_usage(turns)
    auxiliary_usage, auxiliary_usage_complete = _trace_auxiliary_usage(
        auxiliary_turns + auxiliary_failures
    )
    for key, value in auxiliary_usage.items():
        totals[key] += value
    if turns:
        usage_complete = usage_complete and auxiliary_usage_complete
    elif auxiliary_turns:
        usage_complete = auxiliary_usage_complete
    transport_attempts, transport_retries, transport_complete = _trace_transport(
        requests,
        turns + auxiliary_turns + failures,
    )

    return {
        "model_turns": len(turns),
        "model_attempts": len(requests),
        "model_failures": len(failures),
        "auxiliary_model_outcomes": len(auxiliary_turns) + len(auxiliary_failures),
        "transport_attempts": transport_attempts,
        "transport_retries": transport_retries,
        "transport_evidence_complete": transport_complete,
        "billing_ambiguous": (
            not transport_complete
            or not usage_complete
            or bool(transport_retries)
            or any(
                type(event.get("transport_attempts")) is int
                and event["transport_attempts"] > 0
                for event in failures
            )
        ),
        "usage": totals,
        "usage_complete": usage_complete,
        "request_metadata": request_metadata,
        "system_prefix_hashes": prefix_hashes,
        "action_origins": [
            str(event.get("origin", ""))
            for event in actions
            if event.get("action_type") == "tool" and event.get("origin")
        ],
        "tool_name_counts": tool_name_counts,
        "tool_status_counts": tool_status_counts,
        "error_code_counts": error_code_counts,
    }


def _merge_auxiliary_call_evidence(captured, calls):
    """Account for compaction/branch-summary calls omitted from run traces."""
    auxiliary = [
        call
        for call in calls
        if isinstance(call, dict) and call.get("call_kind", "agent") != "agent"
    ]
    if not auxiliary:
        return captured

    recorded = int(captured.get("auxiliary_model_outcomes", 0) or 0)
    if recorded >= len(auxiliary):
        return captured
    auxiliary = auxiliary[recorded:]

    merged = dict(captured)
    merged["usage"] = dict(captured["usage"])
    merged["model_attempts"] += len(auxiliary)
    merged["model_failures"] += sum(
        call.get("completed") is False for call in auxiliary
    )

    auxiliary_usage_complete = True
    for call in auxiliary:
        usage = call.get("usage")
        if not isinstance(usage, dict):
            auxiliary_usage_complete = False
            usage = {}
        if any(
            type(usage.get(required)) is not int
            for required in ("input_tokens", "output_tokens")
        ):
            auxiliary_usage_complete = False
        for key in _LIVE_USAGE_KEYS:
            value = usage.get(key)
            if type(value) is int:
                merged["usage"][key] += value
    merged["usage_complete"] = bool(
        captured["usage_complete"] and auxiliary_usage_complete
    )

    auxiliary_transport_complete = all(
        type(call.get("transport_attempts")) is int
        and type(call.get("transport_retries")) is int
        for call in auxiliary
    )
    transport_complete = bool(
        captured["transport_evidence_complete"] and auxiliary_transport_complete
    )
    merged["transport_evidence_complete"] = transport_complete
    if transport_complete:
        merged["transport_attempts"] = int(captured["transport_attempts"] or 0) + sum(
            call["transport_attempts"] for call in auxiliary
        )
        merged["transport_retries"] = int(captured["transport_retries"] or 0) + sum(
            call["transport_retries"] for call in auxiliary
        )
    else:
        merged["transport_attempts"] = None
        merged["transport_retries"] = None
    merged["billing_ambiguous"] = bool(
        captured["billing_ambiguous"]
        or not merged["usage_complete"]
        or not transport_complete
        or (merged["transport_retries"] or 0) > 0
    )
    return merged


def _terminal_payload(payload):
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    stop_reason = payload.get("stop_reason")
    if (status, stop_reason) in _TERMINAL_STATE_PAIRS:
        return status, stop_reason
    return None


def read_run_terminal_status(run_store, task_state):
    """Return terminal artifact flags plus their consensus status and reason."""
    if task_state is None:
        return "", False, False, False, "", ""
    run_id = str(getattr(task_state, "run_id", "") or "")
    if not run_id:
        return "", False, False, False, "", ""

    try:
        state_payload = json.loads(
            run_store.task_state_path(task_state).read_text(encoding="utf-8")
        )
        task_state_pair = _terminal_payload(state_payload)
    except (
        AttributeError,
        OSError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        task_state_pair = None

    try:
        report_payload = json.loads(
            run_store.report_path(task_state).read_text(encoding="utf-8")
        )
        report_pair = _terminal_payload(report_payload.get("run"))
    except (
        AttributeError,
        OSError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        report_pair = None

    try:
        trace_events = [
            json.loads(line)
            for line in run_store.trace_path(task_state)
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
    except (
        AttributeError,
        OSError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        trace_pair = None
    else:
        terminal_events = [
            event for event in trace_events if event.get("event") == "run_finished"
        ] if all(isinstance(event, dict) for event in trace_events) else []
        trace_pair = (
            _terminal_payload(terminal_events[0])
            if len(terminal_events) == 1
            else None
        )

    consensus = (
        task_state_pair
        if task_state_pair is not None
        and task_state_pair == report_pair == trace_pair
        else ("", "")
    )

    return (
        run_id,
        task_state_pair is not None,
        report_pair is not None,
        trace_pair is not None,
        *consensus,
    )


def _turn_completed(result):
    return (
        result.error_code is None
        and result.task_state_terminal
        and result.report_terminal
        and result.trace_terminal
        and result.terminal_status == "completed"
        and result.stop_reason == "final_answer_returned"
        and isinstance(result.final_answer, str)
        and bool(result.final_answer.strip())
    )


class TurnRunner:
    """Run one turn and retain bounded persisted and in-memory evidence."""

    def __init__(self, pony, config: RunConfig):
        self.pony = pony
        self.config = config

    def run_turn(
        self, turn: int, user_prompt: str, expected_behavior: str
    ) -> TurnResult:
        """Execute one turn and capture only persisted trace/artifact truth."""
        session_before = len(self.pony.session.get("messages", []))
        started_ns = time.monotonic_ns()
        error_code: str | None = None
        exception_error_codes = {}
        final_answer = ""
        stopped_at_step_limit = False
        calls = getattr(self.pony.model_client, "calls", [])
        sniffer_before = len(calls) if isinstance(calls, list) else 0
        previous_task_state = getattr(self.pony, "current_task_state", None)
        previous_run_id = str(getattr(previous_task_state, "run_id", "") or "")

        try:
            final_answer = self.pony.ask(user_prompt)
        except Exception as exc:  # capture and continue; caller decides
            exception_error_codes = _exception_error_code_counts(exc)
            error_code = next(iter(exception_error_codes), "turn_error")

        duration_ms = (time.monotonic_ns() - started_ns) // 1_000_000
        session_after = len(self.pony.session.get("messages", []))

        current_task_state = getattr(self.pony, "current_task_state", None)
        current_run_id = str(getattr(current_task_state, "run_id", "") or "")
        task_state = (
            current_task_state
            if current_run_id and current_run_id != previous_run_id
            else None
        )
        captured = (
            read_turn_trace(self.pony.run_store.trace_path(task_state))
            if task_state is not None
            else _empty_trace_capture()
        )
        calls = getattr(self.pony.model_client, "calls", [])
        new_calls = calls[sniffer_before:] if isinstance(calls, list) else []
        captured = _merge_auxiliary_call_evidence(captured, new_calls)
        for code, count in exception_error_codes.items():
            captured["error_code_counts"][code] = (
                captured["error_code_counts"].get(code, 0) + count
            )
        agent_calls = [
            call
            for call in new_calls
            if isinstance(call, dict)
            and call.get("call_kind", "agent") == "agent"
            and call.get("completed", True) is not False
        ]
        actual_user_contents = tuple(
            str(call.get("last_user_content", "")) for call in agent_calls
        )
        request_metadata_by_call = tuple(captured["request_metadata"])
        metadata = dict(request_metadata_by_call[0]) if request_metadata_by_call else {}
        messages_count = metadata.get("messages_count", 0)
        provider_input_messages_len = (
            messages_count
            if isinstance(messages_count, int) and not isinstance(messages_count, bool)
            else 0
        )
        (
            run_id,
            task_state_terminal,
            report_terminal,
            trace_terminal,
            terminal_status,
            stop_reason,
        ) = (
            read_run_terminal_status(self.pony.run_store, task_state)
        )
        stopped_at_step_limit = stop_reason == "step_limit_reached"

        return TurnResult(
            turn=turn,
            user_prompt=user_prompt,
            expected_behavior=expected_behavior,
            final_answer=final_answer,
            metadata=metadata,
            session_message_count_before=session_before,
            session_message_count_after=session_after,
            model_turns_this_turn=captured["model_turns"],
            model_attempts_this_turn=captured["model_attempts"],
            model_failures_this_turn=captured["model_failures"],
            transport_attempts_this_turn=captured["transport_attempts"],
            transport_retries_this_turn=captured["transport_retries"],
            transport_evidence_complete=captured["transport_evidence_complete"],
            billing_ambiguous=captured["billing_ambiguous"],
            duration_ms=duration_ms,
            usage=captured["usage"],
            stopped_at_step_limit=stopped_at_step_limit,
            error_code=error_code,
            provider_input_messages_len=provider_input_messages_len,
            current_user_content=actual_user_contents[0]
            if actual_user_contents
            else "",
            usage_complete=captured["usage_complete"],
            request_metadata_by_call=request_metadata_by_call,
            system_prefix_hashes=tuple(captured["system_prefix_hashes"]),
            action_origins=tuple(captured["action_origins"]),
            actual_user_contents=actual_user_contents,
            run_id=run_id,
            task_state_terminal=task_state_terminal,
            report_terminal=report_terminal,
            trace_terminal=trace_terminal,
            terminal_status=terminal_status,
            stop_reason=stop_reason,
            tool_name_counts=captured["tool_name_counts"],
            tool_status_counts=captured["tool_status_counts"],
            error_code_counts=captured["error_code_counts"],
        )


@dataclass(frozen=True)
class Assertion:
    """One binary check produced by AssertionEngine."""

    name: str
    passed: bool
    expected: str
    actual: str
    gate: str = ""

    def __post_init__(self):
        if not self.gate:
            object.__setattr__(self, "gate", _assertion_gate(self.name))


def _assertion_gate(name):
    if any(
        part in name for part in ("usage", "tokens_under_cap", "attempts_under_cap")
    ):
        return "transport_cost"
    if any(part in name for part in ("api_key", "artifact_modes")):
        return "security"
    if any(
        part in name
        for part in (
            "session_is_current",
            "tool_pairs",
            "artifacts_terminal",
            "fixture",
        )
    ):
        return "persistence"
    return "behavior"


class AssertionEngine:
    """Turn-scoped hard-assertion engine. Never raises; returns list[Assertion]."""

    def __init__(self, config: RunConfig):
        self.config = config

    def dispatch(self, turn, result: TurnResult, pony, all_results):
        """Route to per-turn check_*.

        ``turn`` may be an int (1..5) or the string ``"global"``.
        """
        if turn == "global":
            return self.check_global(all_results, pony)
        if turn not in {1, 2, 3, 4, 5}:
            return []
        checks = self.check_turn_completion(result)
        if turn == 1:
            return checks + self.check_turn_1_recall(result)
        if turn == 2:
            return checks + self.check_turn_2_digest(result, pony)
        if turn == 3:
            return checks + self.check_turn_3_source_allocator(result)
        if turn == 4:
            return checks + self.check_turn_4_compaction(result, pony)
        if turn == 5:
            return checks + self.check_turn_5_cache_anchor(result, all_results)
        return []

    def check_turn_completion(self, result: TurnResult) -> list[Assertion]:
        terminal_evidence = (
            result.task_state_terminal
            and result.report_terminal
            and result.trace_terminal
        )
        return [
            Assertion(
                name="turn_terminal_evidence_complete",
                passed=terminal_evidence,
                expected="task state, report, and trace all contain terminal evidence",
                actual=str(terminal_evidence),
            ),
            Assertion(
                name="turn_completed_with_final_answer",
                passed=(
                    terminal_evidence
                    and result.terminal_status == "completed"
                    and result.stop_reason == "final_answer_returned"
                ),
                expected="completed/final_answer_returned",
                actual=f"{result.terminal_status}/{result.stop_reason}",
            ),
            Assertion(
                name="turn_final_answer_nonempty",
                passed=(
                    isinstance(result.final_answer, str)
                    and bool(result.final_answer.strip())
                ),
                expected="non-empty final answer",
                actual="present" if str(result.final_answer).strip() else "empty",
            ),
        ]

    def check_turn_1_recall(self, result: TurnResult) -> list[Assertion]:
        """Six assertions verifying recall triggered correctly."""
        m = result.metadata or {}
        allocator = m.get("context_source_allocator") or {}
        injection_tokens = m.get("injection_tokens") or {}
        content = result.current_user_content or ""

        out = []
        allocator_name = allocator.get("name", "")
        out.append(
            Assertion(
                name="priority_allocator_active",
                passed=allocator_name == "priority_allocator",
                expected="priority_allocator",
                actual=str(allocator_name),
            )
        )
        out.append(
            Assertion(
                name="recalled_memory_block_present",
                passed="<pony:recalled_memory" in content,
                expected='"<pony:recalled_memory" in current_user_content',
                actual=("<pony:recalled_memory" in content) and "found" or "not found",
            )
        )
        out.append(
            Assertion(
                name="seed_note_name_visible",
                passed="cache-invariant" in content,
                expected='"cache-invariant" in current_user_content',
                actual=("cache-invariant" in content) and "found" or "not found",
            )
        )
        recall_tokens = int(injection_tokens.get("recalled_memory", 0) or 0)
        out.append(
            Assertion(
                name="recalled_memory_tokens_gt_zero",
                passed=recall_tokens > 0,
                expected="injection_tokens[recalled_memory] > 0",
                actual=str(recall_tokens),
            )
        )
        err_count = int(m.get("recall.error_count", 0) or 0)
        out.append(
            Assertion(
                name="recall_error_count_zero",
                passed=err_count == 0,
                expected="recall.error_count == 0",
                actual=str(err_count),
            )
        )
        final = result.final_answer or ""
        no_error_and_answered = result.error_code is None and bool(final.strip())
        out.append(
            Assertion(
                name="turn_1_completed_without_error",
                passed=no_error_and_answered,
                expected="pony.ask returned without exception",
                actual=result.error_code
                or ("answer present" if final.strip() else "answer empty"),
            )
        )
        return out

    def check_turn_2_digest(self, result: TurnResult, pony) -> list[Assertion]:
        """Verify digest application and native per-call trace evidence.

        Search is restricted to messages added THIS turn (via the
        session-count-before/after window on ``result``). Within that
        window, we prefer the FIRST tool_result whose _pony_meta says
        digest_applied=True — that's the read_file result we intended
        to observe. If none is digested, we fall back to the last
        tool_result in the window so failures still surface a concrete
        actual value.
        """
        out = []
        messages = getattr(pony, "session", {}).get("messages", []) or []
        turn_slice = messages[
            result.session_message_count_before : result.session_message_count_after
        ]
        tool_result_msg = None
        for msg in turn_slice:
            content = msg.get("content")
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                pm = msg.get("_pony_meta") or {}
                if pm.get("digest_applied"):
                    tool_result_msg = msg
                    break
        if tool_result_msg is None:
            for msg in reversed(turn_slice):
                content = msg.get("content")
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                ):
                    tool_result_msg = msg
                    break

        meta = (tool_result_msg or {}).get("_pony_meta") or {}
        digest_applied = bool(meta.get("digest_applied"))
        out.append(
            Assertion(
                name="digest_applied_flag_true",
                passed=digest_applied,
                expected="last tool_result message has _pony_meta.digest_applied=True",
                actual=str(digest_applied),
            )
        )

        # tool_result content should start with [digest]
        tr_content = ""
        if tool_result_msg is not None:
            content = tool_result_msg.get("content") or []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tr_content = str(block.get("content") or "")
                    break
        out.append(
            Assertion(
                name="tool_result_content_starts_with_digest",
                passed=tr_content.strip().startswith("[digest]"),
                expected="tool_result content starts with '[digest]'",
                actual=tr_content[:80] if tr_content else "(empty)",
            )
        )

        source_hash = str(meta.get("source_hash", "") or "")
        valid_source_hash = len(source_hash) == 16 and all(
            char in "0123456789abcdef" for char in source_hash
        )
        expected_raw_result_id = f"raw_result_id: tool_result:{source_hash}"
        out.append(
            Assertion(
                name="tool_result_content_contains_logical_raw_result_id",
                passed=valid_source_hash and expected_raw_result_id in tr_content,
                expected="tool_result content contains its logical raw-result id",
                actual="found" if expected_raw_result_id in tr_content else "not found",
            )
        )

        try:
            run_dir = Path(pony.run_store.run_dir(result.run_id))
        except (AttributeError, TypeError, ValueError):
            run_dir = None
        host_path_hidden = bool(
            run_dir and str(run_dir) not in tr_content and "raw at " not in tr_content
        )
        out.append(
            Assertion(
                name="tool_result_content_hides_host_artifact_path",
                passed=host_path_hidden,
                expected="tool_result content excludes the Host artifact path",
                actual="hidden" if host_path_hidden else "exposed",
            )
        )

        raw_path = (
            run_dir / "tool_results" / f"{source_hash}.txt"
            if run_dir is not None and valid_source_hash
            else None
        )
        raw_exists = bool(raw_path and raw_path.is_file())
        out.append(
            Assertion(
                name="raw_file_exists_on_disk",
                passed=raw_exists,
                expected="trusted raw-result artifact exists",
                actual="exists" if raw_exists else "missing",
            )
        )

        raw_sha256 = ""
        if raw_exists:
            try:
                with raw_path.open("rb") as handle:
                    raw_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
            except OSError:
                raw_sha256 = ""
        visible_sha256 = f"content_sha256: sha256:{raw_sha256}"
        out.append(
            Assertion(
                name="raw_file_digest_matches_visible_sha256",
                passed=bool(
                    raw_sha256
                    and source_hash == raw_sha256[:16]
                    and visible_sha256 in tr_content
                ),
                expected="raw artifact digest matches the model-visible SHA-256",
                actual="matches"
                if raw_sha256 and visible_sha256 in tr_content
                else "mismatch",
            )
        )

        out.append(
            Assertion(
                name="raw_file_source_hash_recorded",
                passed=valid_source_hash,
                expected="_pony_meta.source_hash is a 16-character lowercase hex digest",
                actual=source_hash or "(empty)",
            )
        )

        model_turns = result.model_turns_this_turn
        out.append(
            Assertion(
                name="provider_tool_action_observed",
                passed="native_tool_use" in result.action_origins,
                expected="native_tool_use in action_origins",
                actual=str(result.action_origins),
            )
        )
        out.append(
            Assertion(
                name="native_tool_roundtrip_uses_multiple_model_turns",
                passed=model_turns >= 2,
                expected="model_turns_this_turn >= 2",
                actual=str(model_turns),
            )
        )
        out.append(
            Assertion(
                name="turn_usage_complete",
                passed=result.usage_complete,
                expected="usage_complete is True",
                actual=str(result.usage_complete),
            )
        )

        actual_user_contents = result.actual_user_contents
        contents_cover_calls = (
            model_turns > 0 and len(actual_user_contents) == model_turns
        )
        out.append(
            Assertion(
                name="actual_user_contents_cover_every_model_turn",
                passed=contents_cover_calls,
                expected="one actual_user_content for each provider call",
                actual=(f"contents={len(actual_user_contents)}, calls={model_turns}"),
            )
        )
        call_metadata = result.request_metadata_by_call
        metadata_cover_calls = len(call_metadata) == model_turns and all(
            isinstance(metadata, dict) for metadata in call_metadata
        )
        user_prompt_reached = contents_cover_calls and metadata_cover_calls
        if user_prompt_reached:
            for metadata, content in zip(call_metadata, actual_user_contents):
                injection_tokens = metadata.get("injection_tokens")
                if not isinstance(injection_tokens, dict):
                    user_prompt_reached = False
                    break
                injection_present = any(injection_tokens.values())
                if result.user_prompt not in content or (
                    injection_present and "<system-reminder>" not in content
                ):
                    user_prompt_reached = False
                    break
        out.append(
            Assertion(
                name="injected_user_prompt_reaches_every_model_turn",
                passed=user_prompt_reached,
                expected=(
                    "each call includes the prompt and includes <system-reminder> "
                    "when that call injected context"
                ),
                actual=str(user_prompt_reached),
            )
        )

        cache_keys = result.system_prefix_hashes
        keys_cover_calls = (
            model_turns > 0 and len(cache_keys) == model_turns and all(cache_keys)
        )
        out.append(
            Assertion(
                name="system_prefix_hashes_cover_every_model_turn",
                passed=keys_cover_calls,
                expected="one non-empty system_prefix_hash for each provider call",
                actual=f"keys={len(cache_keys)}, calls={model_turns}",
            )
        )
        keys_stable = keys_cover_calls and len(set(cache_keys)) == 1
        out.append(
            Assertion(
                name="system_prefix_hash_stable_within_turn",
                passed=keys_stable,
                expected="all system_prefix_hash values within the turn are identical",
                actual=str(sorted(set(cache_keys))),
            )
        )
        return out

    def check_turn_3_source_allocator(self, result: TurnResult) -> list[Assertion]:
        """Five assertions verifying bounded whole-chunk source allocation."""
        m = result.metadata or {}
        allocator = m.get("context_source_allocator") or {}
        pool = int(allocator.get("pool_tokens", 0) or 0)
        used = int(allocator.get("used_tokens", 0) or 0)
        source_tokens = allocator.get("source_tokens") or {}
        rows = (m.get("context_breakdown") or {}).get("sources") or []
        hard_caps = {
            str(row.get("name", "")): int(row.get("hard_cap", 0) or 0)
            for row in rows
            if isinstance(row, dict)
        }

        out = []
        out.append(
            Assertion(
                name="priority_allocator_active",
                passed=allocator.get("name") == "priority_allocator",
                expected="context_source_allocator.name == priority_allocator",
                actual=str(allocator.get("name", "")),
            )
        )
        out.append(
            Assertion(
                name="source_pool_positive",
                passed=pool > 0,
                expected="source pool > 0",
                actual=str(pool),
            )
        )
        out.append(
            Assertion(
                name="source_pool_not_exceeded",
                passed=0 <= used <= pool,
                expected="0 <= used_tokens <= pool_tokens",
                actual=f"used={used}, pool={pool}",
            )
        )
        out.append(
            Assertion(
                name="source_totals_match_allocator",
                passed=sum(int(value) for value in source_tokens.values()) == used,
                expected="sum(source_tokens) == used_tokens",
                actual=f"source_tokens={source_tokens}, used={used}",
            )
        )
        caps_respected = all(
            type(value) is int
            and value >= 0
            and source in hard_caps
            and value <= hard_caps[source]
            for source, value in source_tokens.items()
        )
        out.append(
            Assertion(
                name="whole_chunks_respect_source_caps",
                passed=caps_respected and not (m.get("injection_truncated") or {}),
                expected="source token totals <= hard caps and no text truncation",
                actual=(
                    f"source_tokens={source_tokens}, hard_caps={hard_caps}, "
                    f"truncated={m.get('injection_truncated') or {}}"
                ),
            )
        )
        return out

    def check_turn_4_compaction(self, result: TurnResult, pony) -> list[Assertion]:
        """Six assertions verifying compaction without canonical history loss."""
        m = result.metadata or {}
        out = []

        dropped = int(m.get("dropped_messages", 0) or 0)
        out.append(
            Assertion(
                name="no_silent_history_drop",
                passed=dropped == 0,
                expected="dropped_messages == 0",
                actual=str(dropped),
            )
        )

        compaction = (m.get("context_breakdown") or {}).get("compaction") or {}
        entry_id = str(compaction.get("entry_id", "") or "")
        summary_tokens = int(compaction.get("summary_tokens", 0) or 0)
        out.append(
            Assertion(
                name="compaction_summary_active",
                passed=bool(entry_id) and summary_tokens > 0,
                expected="compaction entry and non-empty summary are active",
                actual=f"entry={entry_id!r}, summary_tokens={summary_tokens}",
            )
        )
        reason = str(compaction.get("reason", "") or "")
        out.append(
            Assertion(
                name="compaction_reason_budget_exceeded",
                passed=reason == "budget_exceeded",
                expected="budget_exceeded",
                actual=reason,
            )
        )
        ratio = compaction.get("compression_ratio", 1.0)
        out.append(
            Assertion(
                name="compaction_reduces_active_context",
                passed=type(ratio) in {int, float} and 0 < ratio < 1,
                expected="0 < compression_ratio < 1",
                actual=str(ratio),
            )
        )

        session_msgs = getattr(pony, "session", {}).get("messages", []) or []
        try:
            validate_messages(session_msgs, require_meta=True)
            pairing_actual = "valid canonical messages"
            no_orphan = True
        except MessageValidationError as exc:
            pairing_actual = str(exc)
            no_orphan = False
        out.append(
            Assertion(
                name="no_orphan_tool_use",
                passed=no_orphan,
                expected="canonical tool_use/tool_result pairs are immediately adjacent",
                actual=pairing_actual,
            )
        )

        # Compaction reached the wire while canonical messages remain on disk.
        wire_len = int(result.provider_input_messages_len or 0)
        session_len = len(session_msgs)
        out.append(
            Assertion(
                name="summary_tail_reached_provider_wire",
                passed=wire_len < session_len,
                expected="provider_input_messages_len < len(session.messages)",
                actual=f"wire={wire_len}, session={session_len}",
            )
        )
        return out

    def check_turn_5_cache_anchor(
        self, result: TurnResult, all_results
    ) -> list[Assertion]:
        """Five assertions verifying cache anchor stability + closure."""
        m = result.metadata or {}
        out = []

        # 1. cache_control_breakpoints non-empty
        breakpoints = m.get("cache_control_breakpoints") or []
        out.append(
            Assertion(
                name="cache_control_breakpoints_nonempty",
                passed=len(breakpoints) >= 1,
                expected="cache_control_breakpoints has at least 1 entry",
                actual=str(breakpoints),
            )
        )

        # Cache-token counters are provider-dependent; per-call keys are the
        # portable trace evidence required by this harness.
        cache_keys = [key for item in all_results for key in item.system_prefix_hashes]
        expected_calls = sum(item.model_turns_this_turn for item in all_results)
        keys_present = (
            expected_calls > 0 and len(cache_keys) == expected_calls and all(cache_keys)
        )
        out.append(
            Assertion(
                name="system_prefix_hashes_present_per_call",
                passed=keys_present,
                expected="one non-empty system_prefix_hash per traced provider call",
                actual=f"keys={len(cache_keys)}, calls={expected_calls}",
            )
        )

        # 3. system_prefix_hash identical across every traced call
        stable = keys_present and len(set(cache_keys)) == 1
        out.append(
            Assertion(
                name="system_prefix_hash_stable_across_turns",
                passed=stable,
                expected="all traced system_prefix_hash values are identical and non-empty",
                actual=str(sorted(set(cache_keys))),
            )
        )

        # 4. metadata completeness for the retained v3 cache contract.
        required = {
            "system_prefix_hash",
            "system_tokens",
            "tools_tokens",
            "messages_count",
            "messages_tokens",
            "cache_control_breakpoints",
            "injection_tokens",
            "injection_truncated",
            "injection_dropped",
            "injection_budget",
            "context_source_allocator",
            "recall.error_count",
            "recall.last_error",
            "dropped_messages",
        }
        missing = required - set(m.keys())
        out.append(
            Assertion(
                name="metadata_schema_complete",
                passed=not missing,
                expected="all required request metadata fields present",
                actual=("missing: " + str(sorted(missing)))
                if missing
                else "all present",
            )
        )

        # 5. session-level recall error count remains zero
        # (we walk all_results — each turn should report zero)
        max_err = 0
        for r in all_results:
            max_err = max(
                max_err, int((r.metadata or {}).get("recall.error_count", 0) or 0)
            )
        out.append(
            Assertion(
                name="no_recall_errors_across_session",
                passed=max_err == 0,
                expected="max(recall.error_count) across all turns == 0",
                actual=str(max_err),
            )
        )
        return out

    def check_global(
        self,
        all_results,
        pony,
        artifact_security=None,
    ) -> list[Assertion]:
        """Cross-turn trace, persistence, terminal, and budget invariants."""
        artifact_security = artifact_security or {
            "files_scanned": 0,
            "secret_hits": [],
            "mode_failures": [],
        }
        out = []
        usage_complete = bool(all_results) and all(
            result.usage_complete for result in all_results
        )
        out.append(
            Assertion(
                name="all_turn_usage_complete",
                passed=usage_complete,
                expected="every recorded turn has complete trace usage",
                actual=str(usage_complete),
            )
        )

        action_origins = [
            origin for result in all_results for origin in result.action_origins
        ]
        out.append(
            Assertion(
                name="provider_tool_action_observed",
                passed="native_tool_use" in action_origins,
                expected="at least one action_decoded.origin == native_tool_use",
                actual=str(action_origins),
            )
        )

        session = getattr(pony, "session", {})
        if not isinstance(session, dict):
            session = {}
        record_type = session.get("record_type")
        version = session.get("format_version")
        session_is_current = (
            record_type == SESSION_RECORD_TYPE
            and type(version) is int
            and version == SESSION_FORMAT_VERSION
            and "history" not in session
            and "schema_version" not in session
        )
        out.append(
            Assertion(
                name="in_memory_session_is_current_without_history",
                passed=session_is_current,
                expected="session has current type/version and no obsolete transcript fields",
                actual=f"record_type={record_type!r}, version={version!r}",
            )
        )

        messages = session.get("messages", [])
        try:
            validate_messages(messages, require_meta=True)
            messages_valid = True
            message_actual = "valid canonical messages"
        except MessageValidationError as exc:
            messages_valid = False
            message_actual = str(exc)
        out.append(
            Assertion(
                name="canonical_tool_pairs_immediately_match",
                passed=messages_valid,
                expected="validate_messages(messages, require_meta=True) succeeds",
                actual=message_actual,
            )
        )

        terminal = bool(all_results) and all(
            result.task_state_terminal
            and result.report_terminal
            and result.trace_terminal
            for result in all_results
        )
        out.append(
            Assertion(
                name="all_turn_artifacts_terminal",
                passed=terminal,
                expected="every turn has terminal task_state, report, and trace artifacts",
                actual=str(terminal),
            )
        )

        try:
            tree = pony.session_store.load_tree(session["id"])
            persisted = tree.projection
            persisted_type = tree.header.get("record_type")
            persisted_version = tree.header.get("format_version")
            persisted_valid = (
                persisted_type == SESSION_HEADER_RECORD_TYPE
                and type(persisted_version) is int
                and persisted_version == SESSION_FORMAT_VERSION
                and persisted.get("record_type") == SESSION_RECORD_TYPE
                and persisted.get("format_version") == SESSION_FORMAT_VERSION
                and persisted.get("messages") == session.get("messages")
            )
            persisted_actual = (
                f"record_type={persisted_type!r}, version={persisted_version!r}"
            )
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            persisted_valid = False
            persisted_actual = "persisted JSONL Session Tree unreadable"
        out.append(
            Assertion(
                name="persisted_jsonl_session_tree_is_current",
                passed=persisted_valid,
                expected="JSONL header/projection use the current format and preserve messages",
                actual=persisted_actual,
            )
        )

        total_calls = sum(r.model_attempts_this_turn for r in all_results)
        out.append(
            Assertion(
                name="total_model_attempts_under_cap",
                passed=total_calls <= self.config.max_model_attempts,
                expected=(
                    f"sum(model_attempts_this_turn) <= {self.config.max_model_attempts}"
                ),
                actual=str(total_calls),
            )
        )
        total_tokens = 0
        for r in all_results:
            u = r.usage or {}
            total_tokens += int(u.get("input_tokens", 0) or 0)
            total_tokens += int(u.get("output_tokens", 0) or 0)
        out.append(
            Assertion(
                name="total_tokens_under_cap",
                passed=total_tokens <= self.config.max_total_tokens,
                expected=(
                    f"total input+output tokens <= {self.config.max_total_tokens:,}"
                ),
                actual=str(total_tokens),
            )
        )
        calls = getattr(getattr(pony, "model_client", None), "calls", ())
        provider_clean = bool(calls) and all(
            call.get("payload_secret_clean") is True for call in calls
        )
        out.append(
            Assertion(
                name="provider_payloads_exclude_api_key",
                passed=provider_clean,
                expected=(
                    "every captured Provider payload excludes the selected API key"
                ),
                actual=str(provider_clean),
            )
        )
        artifact_clean = not artifact_security["secret_hits"]
        out.append(
            Assertion(
                name="active_artifacts_exclude_api_key",
                passed=artifact_clean,
                expected=("new or changed .pony artifacts contain no selected API key"),
                actual=str(artifact_security["secret_hits"]),
            )
        )
        private_modes = not artifact_security["mode_failures"]
        out.append(
            Assertion(
                name="active_private_artifact_modes",
                passed=private_modes,
                expected=("active private files are 0600 and directories are 0700"),
                actual=str(artifact_security["mode_failures"]),
            )
        )
        return out


class Reporter:
    """Terminal + JSON reporter for a live-e2e run."""

    _COLOR_GREEN = "\033[32m"
    _COLOR_RED = "\033[31m"
    _COLOR_YELLOW = "\033[33m"
    _COLOR_RESET = "\033[0m"

    def __init__(self, config: RunConfig, output_dir: Path, *, resolution_report):
        self.config = config
        self.output_dir = Path(output_dir)
        self.provider_resolution = _provider_resolution_evidence(
            resolution_report,
            config.provider,
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._use_color = sys.stdout.isatty()

    def _color(self, text: str, color: str) -> str:
        return f"{color}{text}{self._COLOR_RESET}" if self._use_color else text

    def render_turn_summary(self, turn, expected: str, assertions: list) -> None:
        total = len(assertions)
        passed = sum(1 for a in assertions if a.passed)
        color = self._COLOR_GREEN if passed == total else self._COLOR_RED
        label = "PASS" if passed == total else "FAIL"
        turn_str = f"Turn {turn}" if isinstance(turn, int) else str(turn).capitalize()
        # Dot-padded label for alignment
        title = f"[live-e2e] {turn_str}: {expected}"
        pad_target = 55
        pad = "." * max(3, pad_target - len(title))
        print(f"{title} {pad} {self._color(label, color)} ({passed}/{total})")
        # Show individual failed assertions
        for a in assertions:
            if not a.passed:
                print(f"    {self._color('❌', self._COLOR_RED)} {a.name}")
                print(f"       expected: {a.expected}")
                print(f"       actual:   {a.actual}")

    def write_json(
        self,
        all_results: list,
        all_assertions: dict,
        totals: dict,
        wall_time_ms: int,
        *,
        aborted_reason: str | None,
        expected_turn_count: int,
        git_head: str,
        artifact_security=None,
        redactor=redact_artifact,
        forbidden_values=(),
    ) -> Path:
        config = self.config
        artifact_security = artifact_security or {
            "files_scanned": 0,
            "secret_hits": [],
            "mode_failures": [],
        }
        run_id = f"live-e2e-{time.time_ns()}"
        payload = {
            "record_type": "live_e2e_report",
            "format_version": LIVE_E2E_REPORT_FORMAT_VERSION,
            "run_id": run_id,
            "provider": config.provider,
            "model": config.model,
            "provider_resolution": self.provider_resolution,
            "git_head": git_head,
            "aborted_reason": aborted_reason or "",
            "wall_time_ms": wall_time_ms,
            "config": {
                "max_model_attempts": config.max_model_attempts,
                "max_total_tokens": config.max_total_tokens,
                "request_timeout_seconds": config.request_timeout_seconds,
                "max_wall_seconds": config.max_wall_seconds,
            },
            "turns": [
                self._turn_to_json(r, all_assertions.get(r.turn, []))
                for r in all_results
            ],
            "global_assertions": [
                self._assertion_to_json(a) for a in all_assertions.get("global", [])
            ],
            "totals": totals,
        }

        total = 0
        passed = 0
        for asserts_list in all_assertions.values():
            for a in asserts_list:
                total += 1
                if a.passed:
                    passed += 1
        payload["assertion_summary"] = {
            "total": total,
            "passed": passed,
            "failed": total - passed,
        }
        payload["gates"] = self._build_gates(
            all_results,
            all_assertions,
            totals,
            wall_time_ms,
        )
        completed_all_turns = len(all_results) == expected_turn_count
        turn_assertions_present = all(
            bool(all_assertions.get(result.turn)) for result in all_results
        )
        global_assertions_present = bool(all_assertions.get("global"))
        transport_gate = payload["gates"]["transport_cost"]
        usage_only_degraded = (
            transport_gate["status"] == "degraded"
            and transport_gate["usage_complete"] is False
            and transport_gate["billing_ambiguous"] is True
            and transport_gate["transport_evidence_complete"] is True
            and transport_gate["transport_retries"] == 0
        )
        failed_assertions = [
            assertion
            for assertions in all_assertions.values()
            for assertion in assertions
            if not assertion.passed
        ]
        allowed_usage_failures = all(
            assertion.gate == "transport_cost"
            and assertion.name in _DEGRADED_USAGE_ASSERTIONS
            for assertion in failed_assertions
        )
        usage_degradation_proved = usage_only_degraded is bool(failed_assertions)
        required_gates_pass = all(
            gate["status"] == "pass"
            for name, gate in payload["gates"].items()
            if name != "transport_cost"
        )
        payload["overall_pass"] = (
            aborted_reason is None
            and completed_all_turns
            and turn_assertions_present
            and global_assertions_present
            and total > 0
            and allowed_usage_failures
            and usage_degradation_proved
            and required_gates_pass
            and (transport_gate["status"] == "pass" or usage_only_degraded)
        )

        payload["artifact_security"] = {
            "files_scanned": int(artifact_security["files_scanned"]),
            "secret_hits": list(artifact_security["secret_hits"]),
            "mode_failures": list(artifact_security["mode_failures"]),
        }
        safe_payload = redactor(payload)
        serialized = json.dumps(safe_payload, indent=2, ensure_ascii=False)
        if any(str(value) and str(value) in serialized for value in forbidden_values):
            raise SensitiveDataBlockedError(
                "live report contains blocked sensitive material"
            )
        report_path = self.output_dir / f"{run_id}.json"
        ensure_private_dir(self.output_dir)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=report_path.name + ".",
            dir=self.output_dir,
        )
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

    def _turn_to_json(self, r, assertions) -> dict:
        return {
            "turn": r.turn,
            "expected_behavior": r.expected_behavior,
            "duration_ms": r.duration_ms,
            "model_attempts": r.model_attempts_this_turn,
            "model_turns": r.model_turns_this_turn,
            "model_failures": r.model_failures_this_turn,
            "transport_attempts": r.transport_attempts_this_turn,
            "transport_retries": r.transport_retries_this_turn,
            "transport_retry_reason_counts": (
                None
                if not r.transport_evidence_complete
                else {"unknown": r.transport_retries_this_turn}
                if r.transport_retries_this_turn
                else {}
            ),
            "transport_evidence_complete": r.transport_evidence_complete,
            "billing_ambiguous": r.billing_ambiguous,
            "stopped_at_step_limit": r.stopped_at_step_limit,
            "terminal_status": r.terminal_status,
            "stop_reason": r.stop_reason,
            "tool_name_counts": dict(r.tool_name_counts),
            "tool_status_counts": dict(r.tool_status_counts),
            "error_code_counts": dict(r.error_code_counts),
            "error_code": (
                r.error_code
                if _stable_trace_label(r.error_code)
                else "turn_error"
                if r.error_code
                else ""
            ),
            "usage": r.usage,
            "usage_complete": r.usage_complete,
            "assertions": [self._assertion_to_json(a) for a in assertions],
        }

    @staticmethod
    def _assertion_to_json(a) -> dict:
        return {
            "name": a.name,
            "gate": a.gate,
            "passed": a.passed,
        }

    def _build_gates(self, all_results, all_assertions, totals, wall_time_ms):
        assertions = [item for group in all_assertions.values() for item in group]

        def assertions_pass(gate, *, ignored_names=frozenset()):
            selected = [item for item in assertions if item.gate == gate]
            required = [item for item in selected if item.name not in ignored_names]
            return bool(required) and all(item.passed for item in required)

        model_attempts = sum(item.model_attempts_this_turn for item in all_results)
        model_turns = sum(item.model_turns_this_turn for item in all_results)
        model_failures = sum(item.model_failures_this_turn for item in all_results)
        turns_completed = bool(all_results) and all(
            _turn_completed(item) for item in all_results
        )
        evidence_complete = bool(all_results) and all(
            item.transport_evidence_complete for item in all_results
        )
        usage_complete = bool(all_results) and all(
            item.usage_complete for item in all_results
        )
        transport_attempts = (
            sum(item.transport_attempts_this_turn or 0 for item in all_results)
            if evidence_complete
            else None
        )
        transport_retries = (
            sum(item.transport_retries_this_turn or 0 for item in all_results)
            if evidence_complete
            else None
        )
        transport_failed = (
            not assertions_pass(
                "transport_cost",
                ignored_names=_DEGRADED_USAGE_ASSERTIONS,
            )
            or not evidence_complete
            or model_attempts > self.config.max_model_attempts
            or wall_time_ms > self.config.max_wall_seconds * 1000
        )
        transport_degraded = not transport_failed and (
            not usage_complete
            or bool(transport_retries)
            or any(item.billing_ambiguous for item in all_results)
        )
        return {
            "behavior": {
                "status": "pass"
                if assertions_pass("behavior") and not model_failures and turns_completed
                else "fail",
                "model_turns": model_turns,
                "model_failures": model_failures,
                "turns_completed": turns_completed,
            },
            "transport_cost": {
                "status": "fail"
                if transport_failed
                else "degraded"
                if transport_degraded
                else "pass",
                "model_attempts": model_attempts,
                "model_attempt_cap": self.config.max_model_attempts,
                "transport_attempts": transport_attempts,
                "transport_retries": transport_retries,
                "transport_retry_reason_counts": (
                    None
                    if transport_retries is None
                    else {"unknown": transport_retries}
                    if transport_retries
                    else {}
                ),
                "transport_evidence_complete": evidence_complete,
                "usage_complete": usage_complete,
                "billing_ambiguous": any(
                    item.billing_ambiguous for item in all_results
                ),
                "input_tokens": totals.get("input_tokens", 0),
                "output_tokens": totals.get("output_tokens", 0),
            },
            "security": {
                "status": "pass" if assertions_pass("security") else "fail",
            },
            "persistence": {
                "status": "pass" if assertions_pass("persistence") else "fail",
            },
        }

    def render_final(
        self,
        overall_pass: bool,
        totals: dict,
        wall_time_ms: int,
        report_path: Path,
        assertion_summary: tuple,
        gates=None,
    ) -> None:
        passed, total = assertion_summary
        gates = gates or {}
        transport = gates.get("transport_cost", {})
        usage_degraded = (
            overall_pass
            and transport.get("status") == "degraded"
            and transport.get("usage_complete") is False
        )
        color = (
            self._COLOR_YELLOW
            if usage_degraded
            else self._COLOR_GREEN
            if overall_pass
            else self._COLOR_RED
        )
        label = (
            "PASS WITH DEGRADED USAGE"
            if usage_degraded
            else "ALL PASS"
            if overall_pass
            else "FAIL"
        )
        for gate_name, display_name in (
            ("behavior", "Behavior"),
            ("transport_cost", "Transport"),
            ("security", "Security"),
            ("persistence", "Persistence"),
        ):
            status = str(gates.get(gate_name, {}).get("status", "unknown")).upper()
            print(f"[live-e2e] {display_name}: {status}")
        print(
            "[live-e2e] Transport evidence: "
            f"model attempts {transport.get('model_attempts', 0)} "
            f"(cap {transport.get('model_attempt_cap', self.config.max_model_attempts)}) · "
            f"HTTP attempts {transport.get('transport_attempts')} · "
            f"retries {transport.get('transport_retries')}"
        )
        print(
            "[live-e2e] Provider resolution: "
            f"{self.provider_resolution['status']} · "
            f"probe calls {self.provider_resolution['model_calls']}"
        )
        print(
            f"[live-e2e] OVERALL: {self._color(label, color)} · {passed}/{total} assertions"
        )
        print(
            f"[live-e2e] wall_time={wall_time_ms / 1000:.1f}s · "
            f"input_tokens={totals.get('input_tokens', 0):,} · "
            f"output_tokens={totals.get('output_tokens', 0):,} · "
            f"cache_reads={totals.get('cache_read_input_tokens', 0):,}"
        )
        print(f"[live-e2e] report: {report_path}")


def warn_if_dirty_working_tree(root: Path) -> None:
    """Print a warning if `git status` reports uncommitted work. Never aborts."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode == 0 and result.stdout.strip():
        print(
            "[live-e2e] warning: working tree not clean; live e2e might interact with your changes",
            file=sys.stderr,
        )


def _provider_call_kind(system):
    text = "\n".join(
        str(block.get("text", "")) for block in system if isinstance(block, dict)
    )
    if any(
        marker in text
        for marker in (
            "You compact coding-agent history",
            "You summarize the prefix of one oversized coding-agent turn",
            "Summarize an abandoned coding-agent branch",
        )
    ):
        return "session_summary"
    return "agent"


class _SniffingProviderWrapper:
    """Capture bounded in-memory call evidence around one real Provider client."""

    def __init__(self, inner, *, forbidden_values=()):
        self._inner = inner
        self._forbidden_values = tuple(str(value) for value in forbidden_values)
        self.calls: list[dict] = []
        self.supports_prompt_cache = getattr(inner, "supports_prompt_cache", False)
        self.provider_binding = getattr(inner, "provider_binding", None)
        self.provider_metadata = getattr(inner, "provider_metadata", {})
        self.provider_resolution_metadata = getattr(
            inner,
            "provider_resolution_metadata",
            {},
        )
        self.last_completion_metadata = {}
        self.last_transport_attempts = 0
        self.last_stop_reason = None

    def complete(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        last_user = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    last_user = content
                    break
                # tool_result carriers have list content — skip
        serialized = json.dumps(
            {"system": system, "tools": tools, "messages": messages},
            ensure_ascii=False,
        )
        payload_secret_clean = all(
            not value or value not in serialized for value in self._forbidden_values
        )
        call = {
            "last_user_content": last_user,
            "call_ts_ns": time.monotonic_ns(),
            "payload_secret_clean": payload_secret_clean,
            "call_kind": _provider_call_kind(system),
            "completed": False,
            "usage": {},
            "transport_attempts": None,
            "transport_retries": None,
        }
        self.calls.append(call)
        if not payload_secret_clean:
            raise SensitiveDataBlockedError(
                "live provider payload contains blocked sensitive material"
            )
        try:
            response = self._inner.complete(
                system=system,
                tools=tools,
                messages=messages,
                max_tokens=max_tokens,
                cache_breakpoints=cache_breakpoints,
            )
            call["completed"] = True
            call["usage"] = dict(getattr(response, "usage", None) or {})
            return response
        finally:
            self.last_completion_metadata = dict(
                getattr(self._inner, "last_completion_metadata", {}) or {}
            )
            self.last_transport_attempts = getattr(
                self._inner, "last_transport_attempts", None
            )
            call["transport_attempts"] = self.last_transport_attempts
            call["transport_retries"] = (
                max(0, self.last_transport_attempts - 1)
                if type(self.last_transport_attempts) is int
                else None
            )
            self.last_stop_reason = getattr(self._inner, "last_stop_reason", None)


def make_live_client(config: RunConfig, *, settings=None, inner=None):
    """Instantiate the selected live client using its production transport."""

    settings = settings or provider_settings(config.repo_root)
    if inner is None:
        from pony.providers.factory import build_transport_client

        inner = build_transport_client(
            settings["transport"],
            model=config.model,
            base_url=settings["base_url"],
            api_key=settings["api_key"],
            timeout=config.request_timeout_seconds,
            auth_mode=settings["auth_mode"],
            capabilities=settings["capabilities"],
        )
    return _SniffingProviderWrapper(
        inner,
        forbidden_values=(settings["api_key"],),
    )


def _budget_exceeded(
    all_results: list, config: RunConfig, wall_start_ns: int
) -> str | None:
    """Return a short reason string if any cost guard has fired; else None."""
    total_attempts = sum(r.model_attempts_this_turn for r in all_results)
    if total_attempts > config.max_model_attempts:
        return (
            "max_model_attempts exceeded "
            f"({total_attempts}>{config.max_model_attempts})"
        )
    total_tokens = 0
    for r in all_results:
        u = r.usage or {}
        total_tokens += int(u.get("input_tokens", 0) or 0)
        total_tokens += int(u.get("output_tokens", 0) or 0)
    if total_tokens > config.max_total_tokens:
        return f"max_total_tokens exceeded ({total_tokens}>{config.max_total_tokens})"
    elapsed_s = (time.monotonic_ns() - wall_start_ns) / 1e9
    if elapsed_s > config.max_wall_seconds:
        return f"max_wall_seconds exceeded ({elapsed_s:.0f}>{config.max_wall_seconds})"
    return None


def _runtime_types():
    from pony.runtime.application import Pony
    from pony.state.session_store import SessionStore
    from pony.workspace.context import WorkspaceContext

    return Pony, SessionStore, WorkspaceContext


def do_reset(repo_root: Path) -> int:
    """Remove leftover seed note, restore pony.toml backup, clear results/*.json."""
    removed = []
    seed = repo_root / SEED_NOTE_REL
    if seed.exists():
        seed.unlink()
        removed.append(str(seed.relative_to(repo_root)))

    digest_target = repo_root / TOOL_DIGEST_FIXTURE_REL
    if (
        digest_target.is_file()
        and not digest_target.is_symlink()
        and digest_target.read_text(encoding="utf-8") == TOOL_DIGEST_FIXTURE_TEXT
    ):
        digest_target.unlink()
        removed.append(str(digest_target.relative_to(repo_root)))

    backup = repo_root / BACKUP_REL
    pony_toml = repo_root / PONY_TOML_REL
    if backup.exists():
        pony_toml.write_bytes(backup.read_bytes())
        backup.unlink()
        removed.append(f"restored {pony_toml.name} from backup")
    elif pony_toml.exists():
        # No backup means the fixture pony.toml is the only one — delete it
        # if it matches the fixture; else leave alone.
        current = pony_toml.read_text(encoding="utf-8")
        if current == FIXTURE_PONY_TOML:
            pony_toml.unlink()
            removed.append(f"deleted fixture {pony_toml.name}")

    results_dir = repo_root / "benchmarks" / "live_e2e" / "results"
    if results_dir.exists():
        for j in results_dir.glob("*.json"):
            j.unlink()
            removed.append(str(j.relative_to(repo_root)))

    if removed:
        print("[live-e2e] --reset cleaned:")
        for item in removed:
            print(f"  - {item}")
    else:
        print("[live-e2e] --reset: nothing to clean")
    return 0


def main() -> int:
    try:
        config = parse_args()
    except ValueError as exc:
        print(f"[live-e2e] {exc}, aborted", file=sys.stderr)
        return 2
    repo_root = config.repo_root
    if config.reset:
        return do_reset(repo_root)

    verify_pony_repo(repo_root)
    warn_if_dirty_working_tree(repo_root)
    if any(
        os.path.lexists(repo_root / relative)
        for relative in (SEED_NOTE_REL, TOOL_DIGEST_FIXTURE_REL)
    ):
        print(
            "[live-e2e] live fixture already exists — run with --reset first",
            file=sys.stderr,
        )
        return 2

    try:
        Pony, SessionStore, WorkspaceContext = _runtime_types()
    except Exception:
        print("[live-e2e] pony runtime import failed", file=sys.stderr)
        return 4

    project_env = read_project_env(repo_root)
    process_env = dict(os.environ)
    wall_start = time.monotonic_ns()
    try:
        resolved_client, settings, resolution_report = resolve_live_provider(
            config,
            project_env=project_env,
            process_env=process_env,
        )
    except Exception as exc:
        raw_code = getattr(exc, "code", "") or (
            str(exc) if isinstance(exc, ValueError) else ""
        )
        code = raw_code if _stable_trace_label(raw_code) else "provider_detection_failed"
        print(f"[live-e2e] {code}, aborted", file=sys.stderr)
        return 2
    config = replace(
        config,
        provider=settings["provider"],
        model=settings["model"],
    )
    check_env(config, settings=settings)
    selected_api_key = settings["api_key"].strip()
    selected_api_key_name = settings["api_key_env"]

    pony_root = repo_root / ".pony"
    artifact_baseline = snapshot_private_artifacts(pony_root)

    digest_fixture = TOOL_DIGEST_FIXTURE_REL.as_posix()
    tool_prompt = (
        "Call the API-provided native read_file tool exactly once for "
        f"{digest_fixture}. After its result, do not call any tool again. The result "
        "may be a [digest] summary; treat that digest as complete evidence and return "
        "a concise final summary. Do not emit XML tool text."
    )
    turns = [
        (
            1,
            "Read the recalled cache-invariant Memory note with exactly one "
            "memory_read call. Do not call any other tool. After its result, return "
            "a concise summary.",
            "recall_triggered",
        ),
        (
            2,
            tool_prompt,
            "provider_tool_roundtrip",
        ),
        (
            3,
            "Do not call any tool. Return exactly: source allocator metadata checked.",
            "source_pool_bounded",
        ),
        (4, "总结一下我们目前讨论的所有内容", "history_compacted"),
        (5, "最后 done", "cache_anchor_verified"),
    ]

    fixture = FixtureManager(repo_root, forbidden_values=(selected_api_key,))
    with fixture:
        model_client = make_live_client(
            config,
            settings=settings,
            inner=resolved_client,
        )
        workspace = WorkspaceContext.build(repo_root)
        session_store = SessionStore(repo_root / ".pony" / "sessions")
        try:
            pony = Pony(
                model_client=model_client,
                workspace=workspace,
                session_store=session_store,
                options=RuntimeOptions(
                    read_only=True,
                    project_trusted=True,
                    max_steps=3,
                    allowed_tools=("read_file", "memory_read"),
                ),
            )
        except Exception:
            print("[live-e2e] pony construction failed", file=sys.stderr)
            return 4

        runner = TurnRunner(pony, config)
        reporter = Reporter(
            config,
            repo_root / "benchmarks" / "live_e2e" / "results",
            resolution_report=resolution_report,
        )
        engine = AssertionEngine(config)

        all_results: list = []
        all_assertions: dict = {}
        aborted_reason: str | None = None

        for turn_no, prompt, expected in turns:
            if turn_no == 4:
                seed_compaction_fixture(pony)
            result = runner.run_turn(turn_no, prompt, expected)
            if result.error_code is not None:
                all_results.append(result)
                all_assertions[turn_no] = []
                print(f"[live-e2e] turn {turn_no} failed", file=sys.stderr)
                aborted_reason = f"provider_error_turn_{turn_no}"
                break
            all_results.append(result)
            turn_asserts = engine.dispatch(turn_no, result, pony, all_results)
            all_assertions[turn_no] = turn_asserts
            reporter.render_turn_summary(turn_no, expected, turn_asserts)

            if not _turn_completed(result):
                aborted_reason = f"turn_not_completed_{turn_no}"
                print(
                    f"[live-e2e] turn {turn_no} did not return a completed final answer",
                    file=sys.stderr,
                )
                break

            reason = _budget_exceeded(all_results, config, wall_start)
            if reason:
                aborted_reason = reason
                print(f"[live-e2e] budget guard fired: {reason}", file=sys.stderr)
                break

        artifact_security = scan_active_private_artifacts(
            pony_root,
            artifact_baseline,
            forbidden_values=(selected_api_key,),
        )
        global_asserts = engine.check_global(
            all_results,
            pony,
            artifact_security,
        )
        all_assertions["global"] = global_asserts
        reporter.render_turn_summary("global", "cross-turn invariants", global_asserts)

        totals = {
            "model_attempts": sum(r.model_attempts_this_turn for r in all_results),
            "model_turns": sum(r.model_turns_this_turn for r in all_results),
            "model_failures": sum(r.model_failures_this_turn for r in all_results),
            "transport_attempts": (
                sum(r.transport_attempts_this_turn or 0 for r in all_results)
                if all(r.transport_evidence_complete for r in all_results)
                else None
            ),
            "transport_retries": (
                sum(r.transport_retries_this_turn or 0 for r in all_results)
                if all(r.transport_evidence_complete for r in all_results)
                else None
            ),
            "input_tokens": sum(
                int((r.usage or {}).get("input_tokens", 0) or 0) for r in all_results
            ),
            "output_tokens": sum(
                int((r.usage or {}).get("output_tokens", 0) or 0) for r in all_results
            ),
            "cache_creation_input_tokens": sum(
                int((r.usage or {}).get("cache_creation_input_tokens", 0) or 0)
                for r in all_results
            ),
            "cache_read_input_tokens": sum(
                int((r.usage or {}).get("cache_read_input_tokens", 0) or 0)
                for r in all_results
            ),
        }

    restoration = fixture.restoration_status()
    fixture_assertion = Assertion(
        name="fixture_restored_after_context_exit",
        passed=restoration["restored"],
        expected="fixture and backup restored after context exit",
        actual=str(restoration["cleanup_error_codes"]),
        gate="persistence",
    )
    all_assertions["global"].append(fixture_assertion)
    reporter.render_turn_summary("fixture", "fixture restoration", [fixture_assertion])

    wall_time_ms = (time.monotonic_ns() - wall_start) // 1_000_000
    git_head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    report_path = reporter.write_json(
        all_results,
        all_assertions,
        totals,
        wall_time_ms,
        aborted_reason=aborted_reason,
        expected_turn_count=len(turns),
        git_head=git_head,
        artifact_security=artifact_security,
        redactor=lambda value: redact_artifact(
            value,
            env={selected_api_key_name: selected_api_key},
        ),
        forbidden_values=(selected_api_key,),
    )

    report = load_live_report(report_path)
    total_asserts = report["assertion_summary"]["total"]
    passed_asserts = report["assertion_summary"]["passed"]
    overall_pass = bool(report["overall_pass"])
    reporter.render_final(
        overall_pass=overall_pass,
        totals=totals,
        wall_time_ms=wall_time_ms,
        report_path=report_path,
        assertion_summary=(passed_asserts, total_asserts),
        gates=report["gates"],
    )

    if aborted_reason:
        if aborted_reason.startswith("provider_error"):
            return 3
        if "max_wall_seconds" in aborted_reason:
            return 6
        return 5
    return 0 if overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
