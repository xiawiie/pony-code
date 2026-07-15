import json
import tempfile
from pathlib import Path

from ..features import memory as memorylib
from ..observability import load_run_artifacts
from ..providers.fake import FakeModelClient
from ..runtime import Pico
from ..session_store import SessionStore
from ..workspace import WorkspaceContext
from .experiments_synthetic import run_context_stress_matrix, run_large_scale_memory_experiment
from .metrics_common import (
    CONTEXT_ABLATION_FORMAT_VERSION,
    DEFAULT_CONTEXT_ABLATION_V2_PATH,
    DEFAULT_MEMORY_ABLATION_V2_PATH,
    DEFAULT_RECOVERY_ABLATION_V2_PATH,
    MEMORY_ABLATION_FORMAT_VERSION,
    RECOVERY_ABLATION_FORMAT_VERSION,
    _safe_ratio,
    _utc_timestamp,
)


def _write_json_artifact(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


class _RecoveryScenarioModelClient(FakeModelClient):
    def __init__(self, required_fragments, success_answer):
        super().__init__([])
        self.required_fragments = [str(fragment).lower() for fragment in required_fragments]
        self.success_answer = str(success_answer)

    def complete(
        self, *, system, tools, messages, max_tokens, cache_breakpoints=None
    ):
        request_text = json.dumps(
            {"system": system, "tools": tools, "messages": messages},
            ensure_ascii=False,
        )
        prompt_lower = request_text.lower()
        if all(fragment in prompt_lower for fragment in self.required_fragments):
            output = f"<final>{self.success_answer}</final>"
        else:
            output = "<final>missing recovery state.</final>"
        self.outputs.append(output)
        return super().complete(
            system=system,
            tools=tools,
            messages=messages,
            max_tokens=max_tokens,
            cache_breakpoints=cache_breakpoints,
        )


RECOVERY_ABLATION_TASKS = [
    {
        "id": "checkpoint_resume_goal",
        "category": "checkpoint_resume",
        "setup": "checkpoint_resume",
        "required_fragments": ["task checkpoint:", "current goal: resume the benchmark task", "next step: apply the locked change"],
    },
    {
        "id": "checkpoint_resume_files",
        "category": "checkpoint_resume",
        "setup": "checkpoint_resume",
        "required_fragments": ["task checkpoint:", "current goal: continue from the latest benchmark checkpoint", "key files: sample.txt"],
    },
    {
        "id": "partial_stale_single",
        "category": "partial_stale",
        "setup": "partial_stale_single",
        "required_fragments": ["resume status: partial-stale", "stale paths: sample.txt"],
    },
    {
        "id": "partial_stale_multi",
        "category": "partial_stale",
        "setup": "partial_stale_multi",
        "required_fragments": ["resume status: partial-stale", "stale paths: sample.txt, notes.txt"],
    },
    {
        "id": "workspace_mismatch_fingerprint",
        "category": "workspace_mismatch",
        "setup": "workspace_mismatch",
        "required_fragments": ["resume status: workspace-mismatch", "current goal: recover after workspace drift"],
    },
    {
        "id": "workspace_mismatch_runtime",
        "category": "workspace_mismatch",
        "setup": "workspace_mismatch",
        "required_fragments": ["resume status: workspace-mismatch", "next step: rebuild runtime state from a fresh checkpoint"],
    },
    {
        "id": "partial_success_shell",
        "category": "partial_success_recovery",
        "setup": "partial_success_shell",
        "required_fragments": ["current blocker: tool_partial_success", "next step: inspect the diff before retry"],
    },
    {
        "id": "partial_success_tool",
        "category": "partial_success_recovery",
        "setup": "partial_success_tool",
        "required_fragments": ["current blocker: tool_failed", "next step: retry after checking the workspace state"],
    },
]


def _build_recovery_agent(workspace_root, required_fragments):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".pico" / "sessions")
    return Pico(
        model_client=_RecoveryScenarioModelClient(required_fragments, "recovery state restored."),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=4,
    )


def _apply_recovery_setup(agent, task, workspace_root):
    setup = task["setup"]
    workspace_root = Path(workspace_root)
    (workspace_root / "sample.txt").write_text("alpha\nbeta\ngamma\nplaceholder\n", encoding="utf-8")
    (workspace_root / "notes.txt").write_text("note-one\nnote-two\n", encoding="utf-8")
    summaries = agent.session.setdefault("memory", {}).setdefault("file_summaries", {})

    if setup == "checkpoint_resume":
        agent.memory.remember_file("sample.txt")
        agent._sync_working_memory()
        agent.session["checkpoints"] = {
            "current_id": "ckpt_resume",
            "items": {
                "ckpt_resume": {
                    "checkpoint_id": "ckpt_resume",
                    "parent_checkpoint_id": "",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Resume the benchmark task" if task["id"] == "checkpoint_resume_goal" else "Continue from the latest benchmark checkpoint",
                    "completed": ["Read sample.txt"],
                    "excluded": [],
                    "current_blocker": "",
                    "next_step": "Apply the locked change" if task["id"] == "checkpoint_resume_goal" else "Continue from remembered file anchors",
                    "key_files": [{"path": "sample.txt", "freshness": None}],
                    "freshness": {},
                    "summary": "checkpoint resume benchmark",
                    "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
                }
            },
        }
        if task["id"] == "checkpoint_resume_files":
            agent.session["checkpoints"]["items"]["ckpt_resume"]["key_files"] = [{"path": "sample.txt", "freshness": None}]
        agent.session_store.save(agent.session)
        return

    if setup in {"partial_stale_single", "partial_stale_multi"}:
        memorylib.set_file_summary_dict(summaries, "sample.txt", "sample.txt: cached benchmark summary", workspace_root=agent.root)
        agent.memory.remember_file("sample.txt")
        sample_freshness = summaries["sample.txt"]["freshness"]
        key_files = [{"path": "sample.txt", "freshness": sample_freshness}]
        freshness = {"sample.txt": sample_freshness}
        if setup == "partial_stale_multi":
            memorylib.set_file_summary_dict(summaries, "notes.txt", "notes.txt: cached note summary", workspace_root=agent.root)
            agent.memory.remember_file("notes.txt")
            notes_freshness = summaries["notes.txt"]["freshness"]
            key_files.append({"path": "notes.txt", "freshness": notes_freshness})
            freshness["notes.txt"] = notes_freshness
        agent._sync_working_memory()
        agent.session["checkpoints"] = {
            "current_id": "ckpt_stale",
            "items": {
                "ckpt_stale": {
                    "checkpoint_id": "ckpt_stale",
                    "parent_checkpoint_id": "",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Recover from stale benchmark summaries",
                    "completed": [],
                    "excluded": [],
                    "current_blocker": "",
                    "next_step": "Re-anchor the stale summaries",
                    "key_files": key_files,
                    "freshness": freshness,
                    "summary": "partial stale benchmark",
                    "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
                }
            },
        }
        agent.session_store.save(agent.session)
        (workspace_root / "sample.txt").write_text("alpha\nbeta\nstale-shifted\nplaceholder\n", encoding="utf-8")
        if setup == "partial_stale_multi":
            (workspace_root / "notes.txt").write_text("note-one\nnote-two-shifted\n", encoding="utf-8")
        return

    if setup == "workspace_mismatch":
        agent.session["checkpoints"] = {
            "current_id": "ckpt_workspace",
            "items": {
                "ckpt_workspace": {
                    "checkpoint_id": "ckpt_workspace",
                    "parent_checkpoint_id": "",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Recover after workspace drift",
                    "completed": [],
                    "excluded": [],
                    "current_blocker": "",
                    "next_step": "Rebuild runtime state from a fresh checkpoint",
                    "key_files": [],
                    "freshness": {},
                    "summary": "workspace mismatch benchmark",
                    "runtime_identity": {"workspace_fingerprint": "outdated-workspace-fingerprint"},
                }
            },
        }
        agent.session_store.save(agent.session)
        return

    if setup in {"partial_success_shell", "partial_success_tool"}:
        blocker = "tool_partial_success" if setup == "partial_success_shell" else "tool_failed"
        next_step = "Inspect the diff before retry" if setup == "partial_success_shell" else "Retry after checking the workspace state"
        agent.session["checkpoints"] = {
            "current_id": "ckpt_partial",
            "items": {
                "ckpt_partial": {
                    "checkpoint_id": "ckpt_partial",
                    "parent_checkpoint_id": "",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Recover after partial tool success",
                    "completed": [],
                    "excluded": [],
                    "current_blocker": blocker,
                    "next_step": next_step,
                    "key_files": [{"path": "sample.txt", "freshness": None}],
                    "freshness": {},
                    "summary": "partial success benchmark",
                    "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
                }
            },
        }
        agent.session_store.save(agent.session)


def _run_recovery_task_variant(task, variant):
    with tempfile.TemporaryDirectory(prefix="pico-recovery-ablation-") as temp_dir:
        workspace_root = Path(temp_dir).resolve()
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        agent = _build_recovery_agent(workspace_root, task["required_fragments"])
        _apply_recovery_setup(agent, task, workspace_root)
        if variant == "resume_disabled":
            agent.session["checkpoints"] = {"current_id": "", "items": {}}
            agent.session_store.save(agent.session)
        final_answer = agent.ask("Continue the recovery task.")
        report, trace = load_run_artifacts(
            agent.run_store.root,
            agent.current_task_state.run_id,
        )
        resume_status = str(report.get("recovery", {}).get("status", ""))
        stale_reanchored = any(
            event.get("event") == "checkpoint_created" and event.get("trigger") == "freshness_mismatch"
            for event in trace
        )
        workspace_drift_detected = any(event.get("event") == "runtime_identity_mismatch" for event in trace)
        invalid_resume = task["category"] in {"partial_stale", "workspace_mismatch"}
        return {
            "task_id": task["id"],
            "category": task["category"],
            "variant": variant,
            "resume_status": resume_status,
            "resume_succeeded": final_answer == "recovery state restored.",
            "stale_reanchored": stale_reanchored,
            "workspace_drift_detected": workspace_drift_detected,
            "false_accept": invalid_resume and resume_status == "full-valid",
            "final_answer": final_answer,
        }


def _recovery_variant_summary(rows):
    rows = list(rows)
    stale_rows = [row for row in rows if row["category"] == "partial_stale"]
    drift_rows = [row for row in rows if row["category"] == "workspace_mismatch"]
    invalid_rows = [row for row in rows if row["category"] in {"partial_stale", "workspace_mismatch"}]
    return {
        "resume_success_rate": _safe_ratio(sum(1 for row in rows if row["resume_succeeded"]), len(rows)),
        "stale_reanchor_rate": _safe_ratio(sum(1 for row in stale_rows if row["stale_reanchored"]), len(stale_rows)),
        "workspace_drift_detection_rate": _safe_ratio(sum(1 for row in drift_rows if row["workspace_drift_detected"]), len(drift_rows)),
        "resume_false_accept_rate": _safe_ratio(sum(1 for row in invalid_rows if row["false_accept"]), len(invalid_rows)),
    }


def run_context_ablation_v2(artifact_path=DEFAULT_CONTEXT_ABLATION_V2_PATH, repetitions=5):
    payload = run_context_stress_matrix(repetitions=repetitions)
    artifact = {
        "record_type": "context_ablation_result",
        "format_version": CONTEXT_ABLATION_FORMAT_VERSION,
        "captured_at": _utc_timestamp(),
        "config_count": payload["config_count"],
        "configs": payload["configs"],
        "summary": payload["summary"],
    }
    return _write_json_artifact(artifact_path, artifact)


def run_memory_ablation_v2(artifact_path=DEFAULT_MEMORY_ABLATION_V2_PATH, repetitions=5):
    payload = run_large_scale_memory_experiment(repetitions=repetitions)
    artifact = {
        "record_type": "memory_ablation_result",
        "format_version": MEMORY_ABLATION_FORMAT_VERSION,
        "captured_at": _utc_timestamp(),
        "task_count": payload["task_count"],
        "runs_per_variant": payload["runs_per_variant"],
        "category_counts": payload["category_counts"],
        "variants": payload["variants"],
        "rows": payload["rows"],
    }
    return _write_json_artifact(artifact_path, artifact)


def run_recovery_ablation_v2(artifact_path=DEFAULT_RECOVERY_ABLATION_V2_PATH, repetitions=3):
    repetitions = int(repetitions)
    variants = {"resume_enabled": [], "resume_disabled": []}
    for task in RECOVERY_ABLATION_TASKS:
        for _ in range(repetitions):
            for variant in variants:
                variants[variant].append(_run_recovery_task_variant(task, variant))
    artifact = {
        "record_type": "recovery_ablation_result",
        "format_version": RECOVERY_ABLATION_FORMAT_VERSION,
        "captured_at": _utc_timestamp(),
        "task_count": len(RECOVERY_ABLATION_TASKS),
        "variants": {
            variant: {
                "summary": _recovery_variant_summary(rows),
                "rows": rows,
            }
            for variant, rows in variants.items()
        },
    }
    return _write_json_artifact(artifact_path, artifact)
