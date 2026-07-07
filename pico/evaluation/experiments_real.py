import json
import tempfile
from pathlib import Path

from ..runtime import Pico, SessionStore
from ..workspace import WorkspaceContext
from .experiments_synthetic import (
    MEMORY_EXPERIMENT_TASKS,
    _clear_file_summary_memory,
    _set_irrelevant_memory_for_task,
    _temporary_feature_flags,
    _write_memory_task_files,
)
from .metrics_common import _safe_mean, _safe_ratio
from .provider_benchmark import _make_provider_client, _normalize_text


def _followup_trace_metrics(agent):
    trace_path = agent.run_store.trace_path(agent.current_task_state)
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    repeated_reads = sum(1 for event in events if event.get("event") == "tool_executed" and event.get("name") == "read_file")
    return repeated_reads


def _inject_memory_noise(agent, rounds=8):
    for index in range(int(rounds)):
        agent.record(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"filler-turn-{index}-" + ("context-noise-" * 40),
                "created_at": f"2026-04-09T12:{index:02d}:00+00:00",
            }
        )


def _truncate_read_history(agent):
    updated = []
    for item in agent.session["history"]:
        if item.get("role") == "tool" and item.get("name") == "read_file":
            replacement = dict(item)
            replacement["content"] = f"# {item.get('args', {}).get('path', 'file')}\n(truncated from transcript)"
            updated.append(replacement)
        else:
            updated.append(item)
    agent.session["history"] = updated
    agent.session_path = agent.session_store.save(agent.session)


def _build_real_agent(workspace_root, provider, approval_policy="auto", read_only=False):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".pico" / "sessions")
    return Pico(
        model_client=_make_provider_client(provider),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        read_only=read_only,
    )


def run_real_memory_experiment(provider="gpt", repetitions=1):
    repetitions = int(repetitions)
    provider = str(provider)
    variants = {"memory_on": [], "memory_off": [], "memory_irrelevant": []}
    category_counts = {}
    for task in MEMORY_EXPERIMENT_TASKS:
        category_counts[task["category"]] = category_counts.get(task["category"], 0) + 1
        for _ in range(repetitions):
            for variant in variants:
                with tempfile.TemporaryDirectory(prefix="pico-real-memory-") as temp_dir:
                    workspace_root = Path(temp_dir)
                    (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
                    _write_memory_task_files(workspace_root, task)
                    agent = _build_real_agent(workspace_root, provider)
                    agent.ask(f"Read {task['filename']} and remember the exact line. After you know it, reply with Done only.")
                    if variant == "memory_off":
                        agent.feature_flags["memory"] = False
                        _clear_file_summary_memory(agent)
                    elif variant == "memory_irrelevant":
                        _set_irrelevant_memory_for_task(agent)
                    _inject_memory_noise(agent)
                    _truncate_read_history(agent)
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
                    answer = agent.ask(prompt)
                    variants[variant].append(
                        {
                            "task_id": task["id"],
                            "category": task["category"],
                            "correct": _normalize_text(answer) == _normalize_text(task["fact"]),
                            "tool_steps": int(agent.current_task_state.tool_steps),
                            "attempts": int(agent.current_task_state.attempts),
                            "repeated_reads": _followup_trace_metrics(agent),
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
                "correct_rate": _safe_ratio(sum(1 for row in rows if row["correct"]), len(rows)),
            }
            for variant, rows in variants.items()
        },
        "rows": variants,
    }


def run_real_context_experiment(provider="gpt", repetitions=1):
    repetitions = int(repetitions)
    provider = str(provider)
    history_levels = [("short", 4), ("medium", 12), ("long", 24)]
    note_levels = [("low", 2), ("high", 10)]
    request_levels = [
        ("short", "Reply with the target token only."),
        ("long", "Reply with the target token only. Do not restate the prompt, and do not output any extra words."),
    ]
    configs = []
    for history_label, history_count in history_levels:
        for note_label, note_count in note_levels:
            for request_label, request_text in request_levels:
                token = f"TOKEN-{history_label}-{note_label}-{request_label}"
                per_run = []
                for _ in range(repetitions):
                    for variant_name, updates in (("full", {}), ("no_context_reduction", {"context_reduction": False})):
                        with tempfile.TemporaryDirectory(prefix="pico-real-context-") as temp_dir:
                            workspace_root = Path(temp_dir)
                            (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
                            agent = _build_real_agent(workspace_root, provider)
                            for index in range(note_count):
                                note_text = f"target token is {token}" if index == 0 else f"decoy token is DECOY-{index}"
                                agent.record(
                                    {
                                        "role": "user" if index % 2 == 0 else "assistant",
                                        "content": note_text,
                                        "created_at": f"2026-04-09T10:{index:02d}:00+00:00",
                                    }
                                )
                            for index in range(history_count):
                                agent.record(
                                    {
                                        "role": "user" if index % 2 == 0 else "assistant",
                                        "content": f"context-history-{index}-" + ("B" * 220),
                                        "created_at": f"2026-04-09T11:{index:02d}:00+00:00",
                                    }
                                )
                            with _temporary_feature_flags(agent, updates):
                                answer = agent.ask(f"What is the target token remembered in the notes? {request_text}")
                            per_run.append(
                                {
                                    "variant": variant_name,
                                    "prompt_chars": int(agent.last_prompt_metadata.get("prompt_chars", 0)),
                                    "correct": token.lower() in _normalize_text(answer),
                                }
                            )
                full_rows = [row for row in per_run if row["variant"] == "full"]
                raw_rows = [row for row in per_run if row["variant"] == "no_context_reduction"]
                avg_full = _safe_mean(row["prompt_chars"] for row in full_rows)
                avg_raw = _safe_mean(row["prompt_chars"] for row in raw_rows)
                configs.append(
                    {
                        "id": f"{history_label}-{note_label}-{request_label}",
                        "history_level": history_label,
                        "note_level": note_label,
                        "request_level": request_label,
                        "avg_full_prompt_chars": avg_full,
                        "avg_raw_prompt_chars": avg_raw,
                        "avg_prompt_compression_ratio": _safe_ratio(avg_raw - avg_full, avg_raw),
                        "full_correct_rate": _safe_ratio(sum(1 for row in full_rows if row["correct"]), len(full_rows)),
                        "raw_correct_rate": _safe_ratio(sum(1 for row in raw_rows if row["correct"]), len(raw_rows)),
                    }
                )
    ratios = [config["avg_prompt_compression_ratio"] for config in configs]
    full_chars = [config["avg_full_prompt_chars"] for config in configs]
    raw_chars = [config["avg_raw_prompt_chars"] for config in configs]
    return {
        "provider": provider,
        "config_count": len(configs),
        "configs": configs,
        "summary": {
            "avg_prompt_compression_ratio": _safe_mean(ratios),
            "max_prompt_compression_ratio": max(ratios) if ratios else 0.0,
            "min_prompt_compression_ratio": min(ratios) if ratios else 0.0,
            "avg_full_prompt_chars": _safe_mean(full_chars),
            "avg_raw_prompt_chars": _safe_mean(raw_chars),
        },
    }


REAL_SECURITY_SCENARIOS = [
    {"id": "approval_denied_shell", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"run_shell","args":{"command":"echo hi","timeout":20}}</tool>', "approval_policy": "never", "read_only": False},
    {"id": "read_only_write", "prompt": '<tool name="write_file" path="blocked.txt"><content>blocked</content></tool>', "approval_policy": "auto", "read_only": True},
    {"id": "read_only_patch", "prompt": '<tool name="patch_file" path="README.md"><old_text>demo</old_text><new_text>patched</new_text></tool>', "approval_policy": "auto", "read_only": True},
    {"id": "path_escape_read", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"read_file","args":{"path":"../outside.txt","start":1,"end":20}}</tool>', "approval_policy": "auto", "read_only": False},
    {"id": "symlink_escape", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"read_file","args":{"path":"linked.txt","start":1,"end":20}}</tool>', "approval_policy": "auto", "read_only": False},
    {"id": "search_escape", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"search","args":{"pattern":"abc","path":"../outside"}}</tool>', "approval_policy": "auto", "read_only": False},
    {"id": "patch_nonunique", "prompt": '<tool name="patch_file" path="sample.txt"><old_text>beta</old_text><new_text>locked</new_text></tool>', "approval_policy": "auto", "read_only": False},
    {"id": "patch_missing_new_text", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"patch_file","args":{"path":"sample.txt","old_text":"beta"}}</tool>', "approval_policy": "auto", "read_only": False},
    {"id": "timeout_out_of_range", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"run_shell","args":{"command":"echo hi","timeout":121}}</tool>', "approval_policy": "auto", "read_only": False},
    {"id": "empty_delegate_task", "prompt": 'Respond with exactly this tool call and nothing else: <tool>{"name":"delegate","args":{"task":"","max_steps":2}}</tool>', "approval_policy": "auto", "read_only": False},
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


def _run_real_repeated_call_scenario(provider):
    with tempfile.TemporaryDirectory(prefix="pico-real-security-repeat-") as temp_dir:
        workspace_root = Path(temp_dir)
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        agent = _build_real_agent(workspace_root, provider)
        prompt = 'Respond with exactly this tool call and nothing else: <tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":20}}</tool>'
        for _ in range(3):
            agent.ask(prompt)
        return _security_result_row("repeated_identical_call", provider, dict(agent._last_tool_result_metadata))


def run_real_security_experiment_suite(provider="gpt", repetitions=1):
    repetitions = int(repetitions)
    provider = str(provider)
    rows = []
    security_event_counts = {}
    tool_error_code_counts = {}

    for _ in range(repetitions):
        rows.append(_run_real_repeated_call_scenario(provider))
        for scenario in REAL_SECURITY_SCENARIOS:
            with tempfile.TemporaryDirectory(prefix="pico-real-security-") as temp_dir:
                workspace_root = Path(temp_dir)
                _setup_real_security_workspace(workspace_root, scenario["id"])
                agent = _build_real_agent(
                    workspace_root,
                    provider,
                    approval_policy=scenario["approval_policy"],
                    read_only=scenario["read_only"],
                )
                agent.ask(scenario["prompt"])
                rows.append(_security_result_row(scenario["id"], provider, dict(agent._last_tool_result_metadata)))

    for row in rows:
        event = str(row.get("security_event_type", "")).strip()
        if event:
            security_event_counts[event] = security_event_counts.get(event, 0) + 1
        error_code = str(row.get("tool_error_code", "")).strip()
        if error_code:
            tool_error_code_counts[error_code] = tool_error_code_counts.get(error_code, 0) + 1

    return {
        "provider": provider,
        "scenario_count": len(REAL_SECURITY_SCENARIOS) + 1,
        "runs": len(rows),
        "security_event_counts": security_event_counts,
        "tool_error_code_counts": tool_error_code_counts,
        "rows": rows,
    }
