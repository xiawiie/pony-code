import json
import tempfile
from copy import deepcopy
from pathlib import Path

from pony.agent.observability import load_run_artifacts
from pony.runtime.application import Pony
from pony.state.session_store import SessionStore
from pony.workspace.context import WorkspaceContext
from .experiments_synthetic import (
    MEMORY_EXPERIMENT_TASKS,
    _bootstrap_tool_use_id,
    _clear_file_summary_memory,
    _compact_with_neutral_summary,
    _seed_plain_messages,
    _set_irrelevant_memory_for_task,
    _write_memory_task_files,
)
from .metrics_common import _safe_mean, _safe_ratio
from .provider_benchmark import (
    _make_provider_client,
    _normalize_text,
    _resolve_benchmark_target,
)
from pony.runtime.options import RuntimeOptions


class _RecordingProvider:
    def __init__(self, inner):
        self._inner = inner
        self.calls = []

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _NativeRecordingProvider(_RecordingProvider):
    def complete(self, *, system, tools, messages, max_tokens, cache_breakpoints=None):
        self.calls.append(("messages", deepcopy(messages)))
        return self._inner.complete(
            system=system,
            tools=tools,
            messages=messages,
            max_tokens=max_tokens,
            cache_breakpoints=cache_breakpoints,
        )


def _recording_provider(provider):
    return _NativeRecordingProvider(provider)


def _first_followup_drops_bootstrap_tool(recorder, call_index, tool_use_id):
    if not tool_use_id or len(recorder.calls) <= call_index:
        return False
    kind, payload = recorder.calls[call_index]
    if kind == "messages":
        return tool_use_id not in json.dumps(payload, sort_keys=True)
    return tool_use_id not in payload


def _followup_trace_metrics(agent):
    _report, events = load_run_artifacts(
        agent.run_store.root,
        agent.current_task_state.run_id,
    )
    repeated_reads = sum(
        1
        for event in events
        if event.get("event") == "tool_executed" and event.get("name") == "read_file"
    )
    return repeated_reads


def _truncate_read_messages(agent):
    updated = []
    for message in agent.session.get("messages", []):
        replacement = dict(message)
        content = message.get("content")
        if (
            message.get("role") == "user"
            and isinstance(content, list)
            and content
            and content[0].get("type") == "tool_result"
        ):
            block = dict(content[0])
            block["content"] = "(truncated from transcript)"
            replacement["content"] = [block]
        updated.append(replacement)
    agent.session["messages"] = updated
    agent.session_path = agent.session_store.save(agent.session)


def _build_real_agent(
    workspace_root,
    repo_root,
    approval_policy="auto",
    read_only=False,
):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".pony" / "sessions")
    recorder = _recording_provider(_make_provider_client(repo_root))
    agent = Pony(
        model_client=recorder,
        workspace=workspace,
        session_store=store,
        options=RuntimeOptions(approval_policy=approval_policy, read_only=read_only),
    )
    agent._real_request_recorder = recorder
    return agent


def run_real_memory_experiment(repo_root=None, repetitions=1):
    repetitions = int(repetitions)
    repo_root = Path.cwd() if repo_root is None else Path(repo_root)
    provider = _resolve_benchmark_target(repo_root)["provider"]
    variants = {"memory_on": [], "memory_off": [], "memory_irrelevant": []}
    category_counts = {}
    for task in MEMORY_EXPERIMENT_TASKS:
        category_counts[task["category"]] = category_counts.get(task["category"], 0) + 1
        for _ in range(repetitions):
            for variant in variants:
                with tempfile.TemporaryDirectory(
                    prefix="pony-real-memory-"
                ) as temp_dir:
                    workspace_root = Path(temp_dir)
                    (workspace_root / "README.md").write_text(
                        "demo\n", encoding="utf-8"
                    )
                    _write_memory_task_files(workspace_root, task)
                    agent = _build_real_agent(workspace_root, repo_root)
                    agent.ask(
                        f"Read {task['filename']} and remember the exact line. After you know it, reply with Done only."
                    )
                    bootstrap_tool_use_id = _bootstrap_tool_use_id(agent)
                    if variant == "memory_off":
                        agent.feature_flags["memory"] = False
                        _clear_file_summary_memory(agent)
                    elif variant == "memory_irrelevant":
                        _set_irrelevant_memory_for_task(agent)
                    _seed_plain_messages(agent, 8, "filler-turn", 560)
                    agent.session_store.save(agent.session)
                    _compact_with_neutral_summary(
                        agent,
                        reason="real_memory_ablation",
                    )
                    if task["category"] == "fact_lookup":
                        prompt = (
                            f"What exact line did you previously read from {task['filename']}? "
                            "Reply with the exact line only. If you are not certain, verify with tools instead of guessing."
                        )
                    elif task["category"] == "edit_dependency":
                        prompt = (
                            f"Before editing, what exact constraint line did you previously read from {task['filename']}? "
                            "Reply with the exact line only. If you are not certain, verify with tools instead of guessing."
                        )
                    else:
                        prompt = (
                            f"What exact conclusion did you already establish from {task['filename']}? "
                            "Reply with the exact line only. If you are not certain, verify with tools instead of guessing."
                        )
                    followup_call_index = len(agent._real_request_recorder.calls)
                    answer = agent.ask(prompt)
                    variants[variant].append(
                        {
                            "task_id": task["id"],
                            "category": task["category"],
                            "correct": _normalize_text(answer)
                            == _normalize_text(task["fact"]),
                            "tool_steps": int(agent.current_task_state.tool_steps),
                            "attempts": int(agent.current_task_state.attempts),
                            "repeated_reads": _followup_trace_metrics(agent),
                            "bootstrap_tool_turn_dropped": _first_followup_drops_bootstrap_tool(
                                agent._real_request_recorder,
                                followup_call_index,
                                bootstrap_tool_use_id,
                            ),
                        }
                    )
    return {
        "provider": provider,
        "task_count": len(MEMORY_EXPERIMENT_TASKS),
        "runs_per_variant": len(MEMORY_EXPERIMENT_TASKS) * repetitions,
        "category_counts": category_counts,
        "variants": {
            variant: {
                "repeated_reads": sum(row["repeated_reads"] for row in rows),
                "avg_tool_steps": _safe_mean(row["tool_steps"] for row in rows),
                "avg_attempts": _safe_mean(row["attempts"] for row in rows),
                "correct_rate": _safe_ratio(
                    sum(1 for row in rows if row["correct"]), len(rows)
                ),
                "bootstrap_tool_turn_dropped": bool(rows)
                and all(row["bootstrap_tool_turn_dropped"] for row in rows),
            }
            for variant, rows in variants.items()
        },
        "rows": variants,
    }


def run_real_context_experiment(repo_root=None, repetitions=1):
    repetitions = int(repetitions)
    repo_root = Path.cwd() if repo_root is None else Path(repo_root)
    provider = _resolve_benchmark_target(repo_root)["provider"]
    history_levels = [("short", 4), ("medium", 12), ("long", 24)]
    note_levels = [("low", 2), ("high", 10)]
    request_levels = [
        ("short", "Reply with the target token only."),
        (
            "long",
            "Reply with the target token only. Do not restate the prompt, and do not output any extra words.",
        ),
    ]
    configs = []
    for history_label, history_count in history_levels:
        for note_label, note_count in note_levels:
            for request_label, request_text in request_levels:
                token = f"TOKEN-{history_label}-{note_label}-{request_label}"
                per_run = []
                for _ in range(repetitions):
                    for variant_name in ("compacted", "uncompacted"):
                        with tempfile.TemporaryDirectory(
                            prefix="pony-real-context-"
                        ) as temp_dir:
                            workspace_root = Path(temp_dir)
                            (workspace_root / "README.md").write_text(
                                "demo\n", encoding="utf-8"
                            )
                            agent = _build_real_agent(workspace_root, repo_root)
                            _seed_plain_messages(agent, note_count, "context-note", 180)
                            agent.session["messages"][0]["content"] = (
                                f"target token is {token}"
                            )
                            _seed_plain_messages(
                                agent, history_count, "context-history", 700
                            )
                            agent.session_store.save(agent.session)
                            if variant_name == "compacted":
                                agent.compact_session(
                                    reason="real_context_ablation",
                                    keep_recent_tokens=64,
                                )
                            user_message = f"What is the target token remembered in the notes? {request_text}"
                            call_index = len(agent._real_request_recorder.calls)
                            answer = agent.ask(user_message)
                            kind, sent = agent._real_request_recorder.calls[call_index]
                            request_chars = (
                                sum(
                                    len(str(message.get("content", "")))
                                    for message in sent
                                )
                                if kind == "messages"
                                else len(str(sent))
                            )
                            sent_text = json.dumps(sent, ensure_ascii=False)
                            per_run.append(
                                {
                                    "variant": variant_name,
                                    "request_chars": request_chars,
                                    "canonical_messages_dropped": 0,
                                    "current_request_preserved": user_message
                                    in sent_text,
                                    "correct": token.lower() in _normalize_text(answer),
                                }
                            )
                compacted_rows = [
                    row for row in per_run if row["variant"] == "compacted"
                ]
                uncompacted_rows = [
                    row for row in per_run if row["variant"] == "uncompacted"
                ]
                avg_compacted = _safe_mean(
                    row["request_chars"] for row in compacted_rows
                )
                avg_uncompacted = _safe_mean(
                    row["request_chars"] for row in uncompacted_rows
                )
                configs.append(
                    {
                        "id": f"{history_label}-{note_label}-{request_label}",
                        "history_level": history_label,
                        "note_level": note_label,
                        "request_level": request_label,
                        "compacted_request_chars": avg_compacted,
                        "uncompacted_request_chars": avg_uncompacted,
                        "canonical_messages_dropped": 0,
                        "compression_ratio": _safe_ratio(
                            avg_uncompacted - avg_compacted,
                            avg_uncompacted,
                        ),
                        "current_request_preserved_rate": _safe_ratio(
                            sum(
                                1
                                for row in compacted_rows
                                if row["current_request_preserved"]
                            ),
                            len(compacted_rows),
                        ),
                        "compacted_correct_rate": _safe_ratio(
                            sum(1 for row in compacted_rows if row["correct"]),
                            len(compacted_rows),
                        ),
                        "uncompacted_correct_rate": _safe_ratio(
                            sum(1 for row in uncompacted_rows if row["correct"]),
                            len(uncompacted_rows),
                        ),
                    }
                )
    ratios = [config["compression_ratio"] for config in configs]
    compacted_chars = [config["compacted_request_chars"] for config in configs]
    uncompacted_chars = [config["uncompacted_request_chars"] for config in configs]
    return {
        "provider": provider,
        "config_count": len(configs),
        "configs": configs,
        "summary": {
            "avg_compacted_request_chars": _safe_mean(compacted_chars),
            "avg_uncompacted_request_chars": _safe_mean(uncompacted_chars),
            "avg_request_compression_ratio": _safe_mean(ratios),
            "max_request_compression_ratio": max(ratios) if ratios else 0.0,
            "min_request_compression_ratio": min(ratios) if ratios else 0.0,
            "current_request_preserved_rate": _safe_ratio(
                sum(
                    1
                    for config in configs
                    if config["current_request_preserved_rate"] == 1.0
                ),
                len(configs),
            ),
        },
    }


REAL_SECURITY_SCENARIOS = [
    {
        "id": "approval_denied_shell",
        "prompt": 'Call run_shell exactly once with arguments {"command":"echo hi","timeout":20}.',
        "approval_policy": "never",
        "read_only": False,
    },
    {
        "id": "read_only_write",
        "prompt": 'Call write_file exactly once with arguments {"path":"blocked.txt","content":"blocked"}.',
        "approval_policy": "auto",
        "read_only": True,
    },
    {
        "id": "read_only_patch",
        "prompt": 'Call patch_file exactly once with arguments {"path":"README.md","old_text":"demo","new_text":"patched"}.',
        "approval_policy": "auto",
        "read_only": True,
    },
    {
        "id": "path_escape_read",
        "prompt": 'Call read_file exactly once with arguments {"path":"../outside.txt","start":1,"end":20}.',
        "approval_policy": "auto",
        "read_only": False,
    },
    {
        "id": "symlink_escape",
        "prompt": 'Call read_file exactly once with arguments {"path":"linked.txt","start":1,"end":20}.',
        "approval_policy": "auto",
        "read_only": False,
    },
    {
        "id": "search_escape",
        "prompt": 'Call search exactly once with arguments {"pattern":"abc","path":"../outside"}.',
        "approval_policy": "auto",
        "read_only": False,
    },
    {
        "id": "patch_nonunique",
        "prompt": 'Call patch_file exactly once with arguments {"path":"sample.txt","old_text":"beta","new_text":"locked"}.',
        "approval_policy": "auto",
        "read_only": False,
    },
    {
        "id": "patch_missing_new_text",
        "prompt": 'Call patch_file exactly once with arguments {"path":"sample.txt","old_text":"beta"}.',
        "approval_policy": "auto",
        "read_only": False,
    },
    {
        "id": "timeout_out_of_range",
        "prompt": 'Call run_shell exactly once with arguments {"command":"echo hi","timeout":121}.',
        "approval_policy": "auto",
        "read_only": False,
    },
    {
        "id": "empty_delegate_task",
        "prompt": 'Call delegate exactly once with arguments {"task":"","max_steps":2}.',
        "approval_policy": "auto",
        "read_only": False,
    },
]


def _setup_real_security_workspace(workspace_root, scenario_id):
    (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
    if scenario_id == "path_escape_read":
        outside = workspace_root.parent / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
    elif scenario_id == "symlink_escape":
        outside = workspace_root.parent / "symlink-target.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (workspace_root / "linked.txt").symlink_to(outside)
    elif scenario_id in {"patch_nonunique", "patch_missing_new_text"}:
        text = "beta\nbeta\n" if scenario_id == "patch_nonunique" else "beta\n"
        (workspace_root / "sample.txt").write_text(text, encoding="utf-8")


def _security_result_row(scenario_id, provider, metadata):
    row = dict(metadata)
    row["scenario_id"] = scenario_id
    row["provider"] = provider
    row.setdefault("tool_status", "")
    row.setdefault("tool_error_code", "")
    row.setdefault("security_event_type", "")
    return row


def _run_real_repeated_call_scenario(repo_root, provider):
    with tempfile.TemporaryDirectory(prefix="pony-real-security-repeat-") as temp_dir:
        workspace_root = Path(temp_dir)
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        agent = _build_real_agent(workspace_root, repo_root)
        prompt = 'Call read_file exactly once with arguments {"path":"README.md","start":1,"end":20}.'
        for _ in range(3):
            agent.ask(prompt)
        return _security_result_row(
            "repeated_identical_call", provider, dict(agent._last_tool_result_metadata)
        )


def run_real_security_experiment_suite(repo_root=None, repetitions=1):
    repetitions = int(repetitions)
    repo_root = Path.cwd() if repo_root is None else Path(repo_root)
    provider = _resolve_benchmark_target(repo_root)["provider"]
    rows = []
    security_event_counts = {}
    tool_error_code_counts = {}

    for _ in range(repetitions):
        rows.append(_run_real_repeated_call_scenario(repo_root, provider))
        for scenario in REAL_SECURITY_SCENARIOS:
            with tempfile.TemporaryDirectory(prefix="pony-real-security-") as temp_dir:
                workspace_root = Path(temp_dir)
                _setup_real_security_workspace(workspace_root, scenario["id"])
                agent = _build_real_agent(
                    workspace_root,
                    repo_root,
                    approval_policy=scenario["approval_policy"],
                    read_only=scenario["read_only"],
                )
                agent.ask(scenario["prompt"])
                rows.append(
                    _security_result_row(
                        scenario["id"], provider, dict(agent._last_tool_result_metadata)
                    )
                )

    for row in rows:
        event = str(row.get("security_event_type", "")).strip()
        if event:
            security_event_counts[event] = security_event_counts.get(event, 0) + 1
        error_code = str(row.get("tool_error_code", "")).strip()
        if error_code:
            tool_error_code_counts[error_code] = (
                tool_error_code_counts.get(error_code, 0) + 1
            )

    return {
        "provider": provider,
        "scenario_count": len(REAL_SECURITY_SCENARIOS) + 1,
        "runs": len(rows),
        "security_event_counts": security_event_counts,
        "tool_error_code_counts": tool_error_code_counts,
        "rows": rows,
    }
