from copy import deepcopy
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from pico import docker_sandbox_network_control as network_control
from pico.docker_sandbox import default_image_manifest_path, load_image_manifest
from pico.checkpoint_store import CheckpointStore
from pico.providers.fake import FakeModelClient
from pico.safe_subprocess import build_trusted_executables
from pico.sandbox_apply import SourceApplier, StagingObserver
from pico.sandbox_session import SandboxSessionStore
from scripts import docker_sandbox_release as release


def _blocked_artifact():
    image = load_image_manifest(default_image_manifest_path())
    return release._base_artifact(
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        image,
    )


def test_production_vertical_corpus_is_bound_to_packaged_image():
    image = load_image_manifest(default_image_manifest_path())

    assert image.corpus_digest == release.CORPUS_DIGEST
    assert len(release.MANDATORY_CHECK_IDS) == 37
    assert len(set(release.MANDATORY_CHECK_IDS)) == 37


def test_unreleased_image_artifact_is_exact_blocked_and_zero_mutation():
    image = load_image_manifest(default_image_manifest_path())
    artifact = release.validate_artifact(_blocked_artifact())

    assert artifact["status"] == "blocked"
    assert artifact["reason_code"] == "sandbox_image_not_released"
    assert artifact["mandatory_passed"] == 0
    assert artifact["mandatory_failed"] == 37
    assert artifact["container_calls"] == 0
    assert artifact["target_started_count"] == 0
    assert artifact["image_set_digest"] == image.image_set_digest
    assert artifact["host_fallback_count"] == 0
    assert artifact["residue_count"] == 0
    assert artifact["prepare_network_performed"] is False
    assert artifact["runtime_network_performed"] is False
    assert artifact["state_mutation_performed"] is False
    assert artifact["product_enablement"] is False
    assert artifact["case_evidence"]["execution_status"] == "not_run"
    assert artifact["case_evidence"]["cases"] == []

    with pytest.raises(ValueError, match="did not pass"):
        release.validate_artifact(artifact, require_pass=True)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.__setitem__("unexpected", True),
        lambda value: value.__setitem__("corpus_digest", "sha256:" + "0" * 64),
        lambda value: value["checks"].reverse(),
        lambda value: value["checks"][0].__setitem__("status", "pass"),
        lambda value: value.__setitem__("host_fallback_count", 1),
        lambda value: value["case_evidence"].__setitem__(
            "evidence_digest", "sha256:" + "0" * 64
        ),
        lambda value: value.__setitem__("product_enablement", True),
    ),
)
def test_production_vertical_reader_rejects_incomplete_or_mixed_evidence(mutation):
    artifact = deepcopy(_blocked_artifact())
    mutation(artifact)

    with pytest.raises(ValueError, match="invalid production vertical"):
        release.validate_artifact(artifact)


def test_worker_environment_inherits_only_local_linux_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("DOCKER_HOST", "tcp://example.invalid:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")

    environment = release._worker_environment(tmp_path / "isolated-home")

    assert environment["HOME"] == str(tmp_path / "isolated-home")
    assert environment["XDG_RUNTIME_DIR"] == str(tmp_path / "runtime")
    assert "DOCKER_HOST" not in environment
    assert "DOCKER_CONTEXT" not in environment


def test_local_vertical_exports_only_clean_tracked_head(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "pico-tests@example.invalid"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Pico Tests"],
        cwd=source,
        check=True,
    )
    tracked = source / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=source, check=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (source / "untracked-link").symlink_to(outside)

    exported = release._export_clean_head_source(source, tmp_path / "exported")

    assert (exported / "tracked.txt").read_text(encoding="utf-8") == "tracked\n"
    assert not (exported / "untracked-link").exists()
    tracked.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tracked tree is not clean"):
        release._export_clean_head_source(source, tmp_path / "dirty-export")


def test_candidate_macos_home_points_to_verified_endpoint(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    endpoint = tmp_path / "docker.sock"

    release._candidate_docker_home(home, endpoint, "darwin")

    alias = home / ".docker" / "run" / "docker.sock"
    assert alias.is_symlink()
    assert os.readlink(alias) == str(endpoint)
    assert alias.parent.stat().st_mode & 0o777 == 0o700
    assert alias.parent.parent.stat().st_mode & 0o777 == 0o700


def test_candidate_public_cli_uses_fixed_bounded_process(monkeypatch, tmp_path):
    captured = {}
    result = object()

    def run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return result

    monkeypatch.setattr(release, "_run_bounded_process", run)
    environment = {"HOME": str(tmp_path / "home")}

    assert release._run_candidate_public_cli(tmp_path, environment) is result
    assert captured["argv"][0] == str(Path(os.sys.executable).resolve())
    assert captured["argv"][1:] == [
        "-m",
        "pico",
        "--cwd",
        str(tmp_path),
        "--provider",
        "ollama",
        "--sandbox",
        "repl",
    ]
    assert captured["env"] is environment
    assert captured["timeout"] == 300
    assert captured["max_bytes"] == release.MAX_CANDIDATE_SMOKE_OUTPUT_BYTES
    assert captured["terminate_on_overflow"] is True


def _outcome(**overrides):
    values = {
        "sandbox_outcome": "completed",
        "exit_code": 0,
        "timed_out": False,
        "stdout": b"",
        "stderr": b"",
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "runner_executed": True,
        "target_started": True,
        "cleanup_status": "completed",
        "residue_detected": False,
        "error_code": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _network_control_result(**overrides):
    facts = {
        "control_gateway_reachable": True,
        "control_host_reachable": True,
        "control_peer_dns_reachable": True,
        "control_peer_tcp_reachable": True,
        "control_peer_udp_reachable": True,
        "host_to_guest_denied": True,
        "production_gateway_denied": True,
        "production_host_denied": True,
        "production_peer_dns_denied": True,
        "production_peer_tcp_denied": True,
        "production_peer_udp_denied": True,
    }
    facts.update(overrides)
    return network_control._result(
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        "sha256:" + "3" * 64,
        facts,
        network_control.NetworkControlCleanup("completed", True, 0, 0),
    )


def _network_probe_facts(**overrides):
    facts = {
        "challenge_bound": True,
        "guest_listener_armed": True,
        "guest_loopback_control": True,
        "guest_no_host_connection": True,
        "host_client_control": True,
        "host_listeners_remaining": 0,
        "host_to_guest_denied": True,
        "marker_absent": True,
        "probe_outcome_valid": True,
        "probe_threads_remaining": 0,
        "production_context_cleaned": True,
        "production_network_none": True,
        "public_dns_denied": True,
        "public_tcp_denied": True,
        "public_udp_denied": True,
    }
    facts.update(overrides)
    return facts


def test_network_case_rows_are_exact_sanitized_and_fail_closed():
    rows = release._network_case_rows(
        _network_control_result(),
        _network_probe_facts(),
    )

    assert [row["case_id"] for row in rows] == list(release._NETWORK_CASE_IDS)
    assert [row["status"] for row in rows] == ["pass"] * 6
    encoded = release._canonical_json(rows)
    assert b"172." not in encoded
    assert b"host.docker.internal" not in encoded
    assert b"/private/" not in encoded
    assert b"sha256:" + b"1" * 64 not in encoded

    failed = release._network_case_rows(
        _network_control_result(control_gateway_reachable=False),
        _network_probe_facts(),
    )
    gateway = next(
        row
        for row in failed
        if row["case_id"] == "network.control_gateway_host_reachable"
    )
    assert gateway["status"] == "fail"
    assert gateway["reason_code"] == "network_control_gateway_host_failed"


def test_failed_network_vertical_emits_all_failed_rows_without_raw_endpoints():
    rows = release._failed_network_vertical()

    assert [row["case_id"] for row in rows] == list(release._NETWORK_CASE_IDS)
    assert [row["status"] for row in rows] == ["fail"] * 6
    assert b"host.docker.internal" not in release._canonical_json(rows)


def test_apply_release_vertical_runs_installed_owner_matrix(tmp_path):
    metadata = {
        "engine": {
            "endpoint_hash": "sha256:" + "1" * 64,
            "client_version": "29.5.2",
            "server_version": "29.5.2",
            "api_version": "1.54",
            "profile": "desktop_vm",
            "security_digest": "sha256:" + "2" * 64,
        },
        "image": {
            "reference": "sha256:" + "3" * 64,
            "manifest_digest": "sha256:" + "3" * 64,
            "image_id": "sha256:" + "4" * 64,
            "platform": "linux/arm64",
        },
        "policy": {
            "version": 1,
            "digest": "sha256:" + "5" * 64,
            "network": "none",
            "mount_digest": "sha256:" + "6" * 64,
            "resource_digest": "sha256:" + "7" * 64,
        },
    }

    class Context:
        def __init__(self, source, store, session, project_state_root):
            self.source_root = source
            self.execution_root = session.workspace_view.physical_root
            self.project_state_root = project_state_root
            self.sandbox_state_root = session.state_root
            self.source_apply_state_root = session.state_root
            self.sandbox_session = session
            self.runner = SimpleNamespace(session_store=store)

        def current_session(self):
            return self.runner.session_store.inspect(self.sandbox_state_root)

    def build_context(source, **kwargs):
        store = SandboxSessionStore(kwargs["sandbox_parent"])

        def bootstrap(request):
            git = request.workspace_view.physical_root / ".git"
            git.mkdir()
            (git / "HEAD").write_text(
                "ref: refs/heads/pico-sandbox\n",
                encoding="utf-8",
            )
            return "a" * 40

        session = store.create(
            source,
            pico_session_id=kwargs["pico_session_id"],
            bootstrap_git=bootstrap,
            git_executable=kwargs.get("git_executable"),
            project_state_root=kwargs["project_state_root"],
            **metadata,
        )
        return Context(source, store, session, kwargs["project_state_root"])

    git = build_trusted_executables(tmp_path, names=("git",)).get("git")
    assert git is not None
    rows = release._release_apply_rows(
        tmp_path / "matrix",
        build_context=build_context,
        checkpoint_store=CheckpointStore,
        observer_type=StagingObserver,
        applier_type=SourceApplier,
        git_executable=git,
        package_root=Path(release.inspect.getsourcefile(StagingObserver)).parent,
    )

    assert [row["case_id"] for row in rows] == list(release._APPLY_CASE_IDS)
    assert [row["status"] for row in rows] == ["pass"] * len(rows)
    assert "os.fork" not in release.inspect.getsource(release._release_apply_rows)

    by_id = {row["case_id"]: row["facts"] for row in rows}
    rollback_inventory = by_id["apply.conflict_rollback_guards"][
        "rollback_inventory"
    ]
    rollback_journal = rollback_inventory["journals"][0]
    assert rollback_inventory == {
        "guard_journal_id": "",
        "journals": [rollback_journal],
        "quarantines": [],
    }
    assert rollback_journal["status"] == "apply_failed_rolled_back"

    crash = by_id["apply.crash_reconcile"]
    active_journal = crash["active_inventory"]["journals"][0]
    assert crash["active_inventory"] == {
        "guard_journal_id": active_journal["journal_id"],
        "journals": [active_journal],
        "quarantines": [
            {"journal_id": active_journal["journal_id"], "temp_names": []}
        ],
    }
    assert active_journal["status"] == "applying"
    assert crash["final_inventory"] == {
        "guard_journal_id": "",
        "journals": [
            {
                "journal_id": active_journal["journal_id"],
                "status": "apply_applied",
            }
        ],
        "quarantines": [],
    }
    cleanup_journal = crash["cleanup_pending_inventory"]["journals"][0]
    cleanup_quarantine = crash["cleanup_pending_inventory"]["quarantines"][0]
    assert crash["cleanup_pending_inventory"] == {
        "guard_journal_id": cleanup_journal["journal_id"],
        "journals": [cleanup_journal],
        "quarantines": [cleanup_quarantine],
    }
    assert cleanup_journal["status"] == "apply_applied"
    assert cleanup_quarantine["journal_id"] == cleanup_journal["journal_id"]
    assert len(cleanup_quarantine["temp_names"]) == 1
    assert crash["cleanup_final_inventory"] == {
        "guard_journal_id": "",
        "journals": [cleanup_journal],
        "quarantines": [],
    }
    assert crash["cleanup_child_exit_code"] == 74
    assert crash["cleanup_first_complete"] is False
    assert crash["cleanup_retry_complete"] is True
    assert crash["cleanup_guard_cleared"] is True

    helper = by_id["apply.helper_failure"]
    assert helper == {
        "child_exit_code": 76,
        "inventory": {
            "guard_journal_id": "",
            "journals": [],
            "quarantines": [],
        },
        "lease_reacquired": True,
        "session_state": "pending_review",
        "source_unchanged": True,
    }


def test_apply_case_predicates_recompute_helper_and_inventory_evidence():
    facts = release._passing_apply_facts()
    rows = release._passing_apply_case_rows()

    assert len(rows) == 6
    assert release._case_gate_results(rows)["apply_fault_matrix"] is True

    assert release._apply_case_result(
        "apply.helper_failure", facts["apply.helper_failure"]
    ) == (True, "verified")
    failed_helper = deepcopy(facts["apply.helper_failure"])
    failed_helper["child_exit_code"] = 0
    assert release._apply_case_result("apply.helper_failure", failed_helper) == (
        False,
        "apply_helper_failure_failed",
    )
    failed_rows = deepcopy(rows)
    helper_row = next(
        row for row in failed_rows if row["case_id"] == "apply.helper_failure"
    )
    helper_row.update(
        release._apply_case_row("apply.helper_failure", failed_helper)
    )
    assert release._case_gate_results(failed_rows)["apply_fault_matrix"] is False

    crash = deepcopy(facts["apply.crash_reconcile"])
    crash["active_inventory"]["guard_journal_id"] = "apply_" + "f" * 32
    assert release._apply_case_result("apply.crash_reconcile", crash) == (
        False,
        "apply_crash_reconcile_failed",
    )
    invalid = deepcopy(facts["apply.crash_reconcile"])
    invalid["cleanup_pending_inventory"]["quarantines"][0]["temp_names"] = [
        {}
    ]
    with pytest.raises(ValueError, match="invalid apply case evidence"):
        release._apply_case_result("apply.crash_reconcile", invalid)


def test_runtime_tool_orchestration_requires_exact_runtime_and_recovery_contract(
    tmp_path,
):
    source = tmp_path / "source"
    execution = tmp_path / "execution"
    source.mkdir()
    execution.mkdir()
    (source / "README.md").write_text("runtime source\n", encoding="utf-8")
    snapshot = release._snapshot_tree(source)
    tool_changes = [
        {
            "tool_change_id": "tc_a",
            "owner_id": "runtime_owner",
            "turn_id": "task_runtime",
            "tool_name": "write_file",
            "status": "finalized",
            "sandbox": {"status": "not_applicable"},
        },
        {
            "tool_change_id": "tc_b",
            "owner_id": "runtime_owner",
            "turn_id": "task_runtime",
            "tool_name": "run_shell",
            "status": "finalized",
                "sandbox": {
                    "status": "completed",
                    "execution_plane": "sandbox",
                    "runner_executed": True,
                    "target_started": True,
                    "exit_code": 0,
                    "timed_out": False,
                    "cleanup_status": "completed",
                    "residue_detected": False,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                },
        },
    ]
    checkpoint = {
        "checkpoint_id": "ckpt_runtime",
        "checkpoint_type": "turn",
        "tool_change_ids": ["tc_a", "tc_b"],
        "file_entries": [
            {"path": release._RUNTIME_CANDIDATE_A},
            {"path": release._RUNTIME_CANDIDATE_B},
        ],
    }
    artifact = {
        "entries": [
            {
                "path": release._RUNTIME_CANDIDATE_B,
                "change_kind": "created",
                "classification": "candidate",
                "before": {"exists": False},
                "after": {
                    "sha256": release._RUNTIME_CANDIDATE_HASHES[
                        release._RUNTIME_CANDIDATE_B
                    ],
                    "size": len(release._RUNTIME_CANDIDATE_B_CONTENT.encode()),
                    "blob_ref": release._RUNTIME_CANDIDATE_HASHES[
                        release._RUNTIME_CANDIDATE_B
                    ].removeprefix("sha256:"),
                },
            },
            {
                "path": release._RUNTIME_CANDIDATE_A,
                "change_kind": "created",
                "classification": "candidate",
                "before": {"exists": False},
                "after": {
                    "sha256": release._RUNTIME_CANDIDATE_HASHES[
                        release._RUNTIME_CANDIDATE_A
                    ],
                    "size": len(release._RUNTIME_CANDIDATE_A_CONTENT.encode()),
                    "blob_ref": release._RUNTIME_CANDIDATE_HASHES[
                        release._RUNTIME_CANDIDATE_A
                    ].removeprefix("sha256:"),
                },
            },
        ],
        "counts": {"candidate": 2},
    }

    class Store:
        def acquire(self, _root):
            return None

        def inspect(self, _root):
            return SimpleNamespace(
                state="applied",
                manifest={
                    "lease": None,
                    "cleanup": {"status": "complete"},
                },
            )

    class CheckpointStore:
        def list_tool_change_records(self, strict=False):
            assert strict is True
            return tool_changes

        def load_checkpoint_record(self, checkpoint_id):
            assert checkpoint_id == "ckpt_runtime"
            return checkpoint

        def validate_tool_change_reference_graph(self):
            return 2

    class Agent:
        source_root = source
        execution_root = execution
        current_task_state = SimpleNamespace(
            recovery_checkpoint_id="ckpt_runtime",
            task_id="task_runtime",
            run_id="run_runtime",
        )
        checkpoint_store = CheckpointStore()
        tool_change_owner_id = "runtime_owner"
        recovery_manager = SimpleNamespace(
            preview_restore=lambda checkpoint_id: {
                "checkpoint_id": checkpoint_id,
                "status": "ready",
                "entries": [
                    {
                        "path": release._RUNTIME_CANDIDATE_A,
                        "decision": "restore",
                        "reason": "hash_match",
                        "change_kind": "created",
                        "before_exists": False,
                        "after_hash": release._RUNTIME_CANDIDATE_HASHES[
                            release._RUNTIME_CANDIDATE_A
                        ].removeprefix("sha256:"),
                        "snapshot_eligible": True,
                        "source_tool_change_ids": ["tc_a"],
                    },
                    {
                        "path": release._RUNTIME_CANDIDATE_B,
                        "decision": "restore",
                        "reason": "hash_match",
                        "change_kind": "created",
                        "before_exists": False,
                        "after_hash": release._RUNTIME_CANDIDATE_HASHES[
                            release._RUNTIME_CANDIDATE_B
                        ].removeprefix("sha256:"),
                        "snapshot_eligible": True,
                        "source_tool_change_ids": ["tc_b"],
                    },
                ],
            }
        )
        run_store = SimpleNamespace(
            load_report=lambda _run_id: {
                "model": {"transport_attempts": 0},
                "sandbox": {"host_fallback_count": 0},
            }
        )
        model_client = FakeModelClient([])
        sandbox_context = SimpleNamespace(
            runner=SimpleNamespace(session_store=Store()),
            sandbox_state_root=tmp_path / "state",
        )
        workspace_observer = object()

        def ask(self, prompt):
            assert prompt
            (execution / release._RUNTIME_CANDIDATE_A).write_text(
                release._RUNTIME_CANDIDATE_A_CONTENT,
                encoding="utf-8",
            )
            (execution / release._RUNTIME_CANDIDATE_B).write_text(
                release._RUNTIME_CANDIDATE_B_CONTENT,
                encoding="utf-8",
            )
            self.session = {"messages": []}
            for name, result in (
                ("read_file", "# README.md\n   1: runtime source"),
                (
                    "write_file",
                    "wrote " + release._RUNTIME_CANDIDATE_A + " (92 chars)",
                ),
                (
                    "run_shell",
                    release._RUNTIME_CANDIDATE_A_CONTENT + "1 passed\n",
                ),
                (
                    "read_file",
                    "# "
                    + release._RUNTIME_CANDIDATE_B
                    + "\n   1: "
                    + release._RUNTIME_CANDIDATE_B_CONTENT,
                ),
            ):
                tool_id = "toolu_" + str(len(self.session["messages"]))
                self.session["messages"].extend(
                    (
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "tool_use", "id": tool_id, "name": name}
                            ],
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_id,
                                    "content": result,
                                }
                            ],
                            "_pico_meta": {"tool_status": "ok"},
                        },
                    )
                )
            return "runtime vertical complete"

        def finalize_sandbox_session(self):
            return {
                "status": "diff_ready",
                "session_state": "pending_review",
                "diff_digest": "sha256:" + "1" * 64,
                "artifact": artifact,
            }

    agent = Agent()

    class Applier:
        def __init__(self, context, observer):
            assert context is agent.sandbox_context
            assert observer is agent.workspace_observer

        def apply(self, digest):
            assert digest == "sha256:" + "1" * 64
            for name in (
                release._RUNTIME_CANDIDATE_A,
                release._RUNTIME_CANDIDATE_B,
            ):
                (source / name).write_bytes((execution / name).read_bytes())
            for path in execution.iterdir():
                path.unlink()
            execution.rmdir()
            return {"status": "apply_applied"}

    agent.sandbox_context.current_session = lambda: SimpleNamespace(
        state="applied",
        manifest={
            "lease": None,
            "cleanup": {"status": "complete"},
        },
    )
    result = release._runtime_tool_orchestration(agent, snapshot, Applier)

    assert result["tool_sequence"] == ["write_file", "run_shell"]
    assert result["roundtrip_passed"] is True
    assert result["recovery_preview_passed"] is True
    assert result["trusted_diff_passed"] is True
    assert result["source_pre_apply_unchanged"] is True
    assert result["apply_passed"] is True
    assert result["cleanup_complete"] is True
    assert [row["status"] for row in result["case_rows"]] == ["pass"] * 3


@pytest.mark.parametrize(
    ("initial_state", "expected_event"),
    (("applying", "reconcile"), ("cleanup_pending", "resume_cleanup")),
)
def test_runtime_cleanup_resumes_exceptional_session_states(
    tmp_path,
    initial_state,
    expected_event,
):
    execution = tmp_path / "execution"
    execution.mkdir()
    events = []

    class Store:
        state = initial_state
        lease = None

        def inspect(self, _root):
            return SimpleNamespace(
                state=self.state,
                manifest={
                    "lease": self.lease,
                    "cleanup": {
                        "status": (
                            "complete"
                            if self.state in {"applied", "discarded"}
                            else "pending"
                        )
                    },
                },
            )

        def acquire(self, _root):
            events.append("acquire")
            self.lease = {"owner_nonce": "nonce"}

        def resume_cleanup(self, _root):
            events.append("resume_cleanup")
            self.state = "discarded"
            self.lease = None
            execution.rmdir()

        def release(self, _root, _nonce):
            events.append("release")
            self.lease = None

    store = Store()
    agent = SimpleNamespace(
        execution_root=execution,
        workspace_observer=object(),
        sandbox_context=SimpleNamespace(
            runner=SimpleNamespace(session_store=store),
            sandbox_state_root=tmp_path / "state",
        ),
    )

    class Applier:
        def __init__(self, _context, _observer):
            self.store = SimpleNamespace()

        def reconcile(self):
            events.append("reconcile")
            store.state = "applied"
            store.lease = None
            execution.rmdir()

    assert release._runtime_cleanup_session(agent, Applier) is True
    assert events == ["acquire", expected_event]


def test_runtime_cleanup_helper_failure_is_fail_closed(tmp_path):
    events = []

    class Store:
        def inspect(self, _root):
            events.append("inspect")
            return SimpleNamespace(
                manifest={"lease": {"owner_nonce": "nonce"}},
            )

        def release(self, _root, nonce):
            events.append(("release", nonce))

    agent = SimpleNamespace(
        execution_root=tmp_path / "execution",
        workspace_observer=object(),
        sandbox_context=SimpleNamespace(
            runner=SimpleNamespace(session_store=Store()),
            sandbox_state_root=tmp_path / "state",
        ),
    )

    class BrokenApplier:
        def __init__(self, _context, _observer):
            raise RuntimeError("cleanup helper failed")

    assert release._runtime_cleanup_session(agent, BrokenApplier) is False
    assert events == ["inspect", ("release", "nonce")]


@pytest.mark.parametrize(
    ("attestation_kind", "expected_constructor"),
    (
        ("development", "development"),
        ("product", "public"),
        ("candidate", "public"),
    ),
)
def test_runtime_vertical_helper_exception_uses_authorized_constructor_and_metadata(
    monkeypatch,
    tmp_path,
    attestation_kind,
    expected_constructor,
):
    import pico.config as config_module
    import pico.runtime as runtime_module
    import pico.session_store as session_store_module
    import pico.workspace as workspace_module

    monkeypatch.setattr(config_module, "read_project_env", lambda *_a, **_k: {})
    monkeypatch.setattr(config_module, "load_pico_toml", lambda _root: {})
    monkeypatch.setattr(
        runtime_module,
        "_build_redaction_snapshot",
        lambda *_a, **_k: ({}, (), object()),
    )
    constructor_calls = []

    class PicoFactory:
        def __new__(cls, **kwargs):
            constructor_calls.append(("public", kwargs))
            return SimpleNamespace()

        @classmethod
        def _for_docker_sandbox_development(cls, **kwargs):
            constructor_calls.append(("development", kwargs))
            return SimpleNamespace()

    monkeypatch.setattr(runtime_module, "Pico", PicoFactory)
    monkeypatch.setattr(session_store_module, "SessionStore", lambda *_a, **_k: object())
    workspace_calls = []

    def build_workspace(*args, **kwargs):
        workspace_calls.append((args, kwargs))
        return SimpleNamespace(trusted_executables={})

    monkeypatch.setattr(
        workspace_module.WorkspaceContext,
        "build",
        staticmethod(build_workspace),
    )
    monkeypatch.setattr(release, "_snapshot_tree", lambda _root: "snapshot")
    monkeypatch.setattr(
        release,
        "_runtime_tool_orchestration",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("helper failed")),
    )
    monkeypatch.setattr(release, "_runtime_cleanup_session", lambda *_a, **_k: True)
    context = SimpleNamespace(
        source_root=tmp_path / "source",
        execution_root=tmp_path / "execution",
        project_state_root=tmp_path / "state",
        logical_root="/workspace",
        source_branch="-",
        source_default_branch="main",
        source_status="(unavailable)",
        authorization=SimpleNamespace(attestation_kind=attestation_kind),
        sandbox_session=SimpleNamespace(
            manifest={"pico_session_id": "runtime-session"}
        ),
    )

    result = release._run_runtime_tool_vertical(context)

    assert result["cleanup_complete"] is True
    assert [row["case_id"] for row in result["case_rows"]] == list(
        release._RUNTIME_CASE_IDS
    )
    assert [row["status"] for row in result["case_rows"]] == ["fail"] * 3
    assert [name for name, _kwargs in constructor_calls] == [expected_constructor]
    assert all(
        isinstance(output, dict)
        for output in constructor_calls[0][1]["model_client"].outputs[:4]
    )
    assert workspace_calls[1][1]["branch_override"] == "pico-sandbox"
    assert workspace_calls[1][1]["default_branch_override"] == "pico-sandbox"
    assert (
        workspace_calls[1][1]["status_override"]
        == "sandbox_execution_state_unknown"
    )


def test_runtime_case_predicates_reject_poisoned_shell_recovery_and_diff():
    runtime = {
        "model_client": "FakeModelClient",
        "provider_transport_attempts": 0,
        "tool_sequence": ["read_file", "write_file", "run_shell", "read_file"],
        "tool_statuses": ["ok"] * 4,
        "tool_change_sequence": ["write_file", "run_shell"],
        "tool_change_statuses": ["finalized"] * 2,
        "initial_read_match": True,
        "builtin_write_a_match": True,
        "shell_observed_a": True,
        "final_read_b_match": True,
        "source_pre_apply_unchanged": True,
        "execution_plane": "sandbox",
        "sandbox_outcome": "completed",
        "exit_code": 0,
        "timed_out": False,
        "target_started": True,
        "runner_executed": True,
        "residue_detected": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "cleanup_status": "completed",
        "host_fallback_count": 0,
    }
    assert release._runtime_case_result("runtime.tool_roundtrip", runtime) == (
        True,
        "verified",
    )
    poisoned = dict(runtime, exit_code=17)
    assert release._runtime_case_result(
        "runtime.tool_roundtrip", poisoned
    ) == (False, "runtime_shell_outcome_invalid")

    recovery = {
        "checkpoint_type": "turn",
        "reference_graph_valid": True,
        "preview_status": "ready",
        "entries": [
            {
                "path": path,
                "decision": "restore",
                "reason": "hash_match",
                "change_kind": "created",
                "before_exists": False,
                "after_sha256": release._RUNTIME_CANDIDATE_HASHES[path],
                "snapshot_eligible": True,
                "source_tool": tool,
            }
            for path, tool in sorted(
                (
                    (release._RUNTIME_CANDIDATE_A, "write_file"),
                    (release._RUNTIME_CANDIDATE_B, "run_shell"),
                )
            )
        ],
    }
    assert release._runtime_case_result("runtime.recovery_preview", recovery) == (
        True,
        "verified",
    )
    extra = deepcopy(recovery)
    extra["entries"].append(dict(extra["entries"][0], path="extra.txt"))
    assert release._runtime_case_result("runtime.recovery_preview", extra) == (
        False,
        "runtime_recovery_mismatch",
    )

    diff = {
        "diff_status": "diff_ready",
        "pre_apply_session_state": "pending_review",
        "source_pre_apply_unchanged": True,
        "entries": [
            {
                "path": path,
                "change_kind": "created",
                "classification": "candidate",
                "before_exists": False,
                "after_sha256": release._RUNTIME_CANDIDATE_HASHES[path],
                "size": len(content.encode("utf-8")),
                "blob_bound": True,
            }
            for path, content in sorted(
                (
                    (
                        release._RUNTIME_CANDIDATE_A,
                        release._RUNTIME_CANDIDATE_A_CONTENT,
                    ),
                    (
                        release._RUNTIME_CANDIDATE_B,
                        release._RUNTIME_CANDIDATE_B_CONTENT,
                    ),
                )
            )
        ],
        "apply_status": "apply_applied",
        "final_session_state": "applied",
        "cleanup_status": "complete",
        "lease_released": True,
        "execution_root_absent": True,
        "source_after": [
            {
                "path": path,
                "sha256": release._RUNTIME_CANDIDATE_HASHES[path],
                "size": len(content.encode("utf-8")),
            }
            for path, content in sorted(
                (
                    (
                        release._RUNTIME_CANDIDATE_A,
                        release._RUNTIME_CANDIDATE_A_CONTENT,
                    ),
                    (
                        release._RUNTIME_CANDIDATE_B,
                        release._RUNTIME_CANDIDATE_B_CONTENT,
                    ),
                )
            )
        ],
    }
    assert release._runtime_case_result("runtime.diff_apply_cleanup", diff) == (
        True,
        "verified",
    )
    wrong_hash = deepcopy(diff)
    wrong_hash["entries"][0]["after_sha256"] = "sha256:" + "0" * 64
    assert release._runtime_case_result(
        "runtime.diff_apply_cleanup", wrong_hash
    ) == (False, "runtime_diff_mismatch")
    rows = sorted(
        (
            *release._passing_apply_case_rows(),
            release._runtime_case_row("runtime.tool_roundtrip", runtime),
            release._runtime_case_row("runtime.recovery_preview", recovery),
            release._runtime_case_row("runtime.diff_apply_cleanup", diff),
        ),
        key=lambda item: item["case_id"],
    )
    poisoned_rows = deepcopy(rows)
    diff_row = next(
        row for row in poisoned_rows if row["case_id"] == "runtime.diff_apply_cleanup"
    )
    diff_row["facts"]["entries"][0]["after_sha256"] = (
        "sha256:" + "0" * 64
    )
    binding = release.unbound_release_binding()
    evidence = release._case_evidence(
        "complete",
        "verified",
        poisoned_rows,
        binding,
    )
    with pytest.raises(ValueError, match="case evidence"):
        release._validate_case_evidence(
            evidence,
            binding,
            artifact_status="failed",
        )


def test_case_evidence_recomputes_network_rows_and_parent_gate():
    rows = sorted(
        (
            *release._passing_apply_case_rows(),
            *release._network_case_rows(
                _network_control_result(),
                _network_probe_facts(),
            ),
            *release._failed_runtime_vertical(True)["case_rows"],
        ),
        key=lambda item: item["case_id"],
    )
    binding = release.unbound_release_binding()
    evidence = release._case_evidence(
        "complete",
        "verified",
        rows,
        binding,
    )

    gates = release._validate_case_evidence(
        evidence,
        binding,
        artifact_status="failed",
    )

    assert gates["external_network_denied"] is True
    for mutation in (
        lambda values: values.pop(
            next(
                index
                for index, row in enumerate(values)
                if row["case_id"] == "network.control_cleanup"
            )
        ),
        lambda values: values.append(deepcopy(values[0])),
        lambda values: values.reverse(),
        lambda values: next(
            row
            for row in values
            if row["case_id"] == "network.production_gateway_host_denied"
        )["facts"].__setitem__("production_host_denied", False),
    ):
        poisoned = deepcopy(rows)
        mutation(poisoned)
        poisoned_evidence = release._case_evidence(
            "complete",
            "verified",
            poisoned,
            binding,
        )
        with pytest.raises(ValueError, match="case evidence"):
            release._validate_case_evidence(
                poisoned_evidence,
                binding,
                artifact_status="failed",
            )


def test_bound_release_source_requires_exact_clean_signed_commit(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git = object()
    monkeypatch.setattr(
        release,
        "build_trusted_executables",
        lambda *_args, **_kwargs: {"git": git},
    )
    calls = []
    results = iter(
        (
            SimpleNamespace(returncode=0, stdout=str(source).encode() + b"\n"),
            SimpleNamespace(returncode=0, stdout=b"a" * 40 + b"\n"),
            SimpleNamespace(returncode=0, stdout=b""),
        )
    )
    def run_git(_git, args, **_kwargs):
        calls.append(args)
        return next(results)

    monkeypatch.setattr(release, "run_hardened_git", run_git)

    assert release._verify_release_source(source, "a" * 40) == "a" * 40
    assert calls[-1] == [
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored",
    ]

    dirty = iter(
        (
            SimpleNamespace(returncode=0, stdout=str(source).encode() + b"\n"),
            SimpleNamespace(returncode=0, stdout=b"a" * 40 + b"\n"),
            SimpleNamespace(returncode=0, stdout=b"?? arbitrary.py\n"),
        )
    )
    monkeypatch.setattr(release, "run_hardened_git", lambda *_args, **_kwargs: next(dirty))
    with pytest.raises(ValueError, match="source identity mismatch"):
        release._verify_release_source(source, "a" * 40)

    wrong_commit = iter(
        (
            SimpleNamespace(returncode=0, stdout=str(source).encode() + b"\n"),
            SimpleNamespace(returncode=0, stdout=b"b" * 40 + b"\n"),
            SimpleNamespace(returncode=0, stdout=b""),
        )
    )
    monkeypatch.setattr(
        release,
        "run_hardened_git",
        lambda *_args, **_kwargs: next(wrong_commit),
    )
    with pytest.raises(ValueError, match="source identity mismatch"):
        release._verify_release_source(source, "a" * 40)


def test_runtime_probe_requires_exact_positive_and_negative_behavior():
    facts = {name: True for name in release._RUNTIME_PROBE_FIELDS}
    privilege = {name: True for name in release._PRIVILEGE_PROBE_FIELDS}
    resources = {name: True for name in release._RESOURCE_PROBE_FIELDS}
    raw = release._canonical_json(facts)

    assert release._runtime_probe_facts(_outcome(stdout=raw, stdout_bytes=len(raw))) == facts
    assert all(
        release._runtime_probe_checks(
            facts,
            host_listener_control=True,
            private_paths_absent=True,
            controlled_network_denied=True,
            privilege_facts=privilege,
            resource_facts=resources,
            oom_limited=True,
            disk_watchdog_limited=True,
            workspace_marker_exists=True,
            source_marker_exists=False,
        ).values()
    )
    poisoned = dict(facts, loopback_allowed=False)
    poisoned_raw = release._canonical_json(poisoned)
    assert release._runtime_probe_facts(
        _outcome(stdout=poisoned_raw, stdout_bytes=len(poisoned_raw))
    )["loopback_allowed"] is False
    checks = release._runtime_probe_checks(
        poisoned,
        host_listener_control=True,
        private_paths_absent=True,
        controlled_network_denied=True,
        privilege_facts=privilege,
        resource_facts=resources,
        oom_limited=True,
        disk_watchdog_limited=True,
        workspace_marker_exists=True,
        source_marker_exists=False,
    )
    assert checks["container_loopback_allowed"] is False
    assert checks["external_network_denied"] is True
    assert release._runtime_probe_facts(
        _outcome(stdout=raw, stdout_bytes=len(raw) + 1)
    ) == {}


def test_corpus_v2_digest_binds_every_identity_input():
    payload = release._corpus_payload()

    assert payload["format_version"] == 2
    assert payload["mandatory_check_ids"] == list(release.MANDATORY_CHECK_IDS)
    assert payload["mandatory_security_tests"] == list(
        release.MANDATORY_SECURITY_TESTS
    )
    assert payload["behavior_parameters"]["disk_watchdog_bytes"] == (
        1024 * 1024 * 1024 + 1
    )
    assert payload["behavior_parameters"]["disk_watchdog_file_bytes"] == (
        128 * 1024 * 1024
    )
    assert payload["behavior_parameters"]["disk_watchdog_full_files"] == 8
    assert set(payload["probe_scripts"]) == {
        "disk_watchdog",
        "ephemeral_read",
        "ephemeral_write",
        "oom",
        "output",
        "privilege",
        "process_tree",
        "resource",
        "runtime",
        "sensitive",
        "tools",
        "workspace_crud",
        "workspace_persistence",
    }
    assert set(payload["expected_behavior"]["predicate_source_sha256"]) == {
        "case_evidence",
        "case_evidence_digest",
        "case_evidence_validator",
        "disk_watchdog",
        "ephemeral_facts",
        "json_probe",
        "oom",
        "output",
        "privilege_facts",
        "process_tree_control",
        "process_tree_interrupt",
        "process_tree",
        "process_tree_paths",
        "remove_probe_paths",
        "resource_facts",
        "runtime_case_result",
        "runtime_case_row",
        "runtime_cleanup_session",
        "runtime_failed_vertical",
        "apply_case_result",
        "apply_case_row",
        "apply_failed_vertical",
        "apply_fixture_context",
        "apply_passing_facts",
        "apply_passing_rows",
        "apply_release_rows",
        "apply_vertical",
        "case_gate_results",
        "runtime_tool_orchestration",
        "runtime_tool_vertical",
        "runtime_facts",
        "runtime_checks",
        "sensitive_facts",
        "snapshot_source_tree",
        "tool_facts",
        "workspace_crud_facts",
        "workspace_persistence_facts",
    }
    assert payload["expected_behavior"]["process_modes"] == {
        "control": {"expected_outcome": "completed", "timeout": 30},
        "interrupt": {"expected_outcome": "interrupted", "timeout": 30},
        "normal": {"expected_outcome": "completed", "timeout": 30},
        "timeout": {"expected_outcome": "timeout", "timeout": 1},
    }
    assert payload["expected_behavior"]["unsupported_entry_kinds"] == [
        "symlink",
        "hardlink",
        "fifo",
        "socket",
        "device",
    ]
    assert payload["external_fixtures"] == {
        "device": {
            "argument": "--device-fixture-source",
            "entry_kinds": ["block_device", "character_device"],
            "expected_error": "unsupported_workspace_entry",
            "required": True,
        },
        "mount_boundary": {
            "argument": "--mount-fixture-source",
            "expected_error": "workspace_mount_boundary",
            "required": True,
        },
    }
    assert payload["runtime_tool_vertical"] == {
        "candidate_a": release._RUNTIME_CANDIDATE_A,
        "candidate_a_sha256": "sha256:"
        + release.hashlib.sha256(
            release._RUNTIME_CANDIDATE_A_CONTENT.encode("utf-8")
        ).hexdigest(),
        "candidate_b": release._RUNTIME_CANDIDATE_B,
        "candidate_b_sha256": "sha256:"
        + release.hashlib.sha256(
            release._RUNTIME_CANDIDATE_B_CONTENT.encode("utf-8")
        ).hexdigest(),
        "shell_command": release._RUNTIME_SHELL_COMMAND,
    }
    assert release._corpus_digest(payload) == release.CORPUS_DIGEST
    for owner in (
        "case_evidence",
        "case_evidence_digest",
        "case_evidence_validator",
        "snapshot_source_tree",
    ):
        assert release._SHA256_RE.fullmatch(
            payload["expected_behavior"]["predicate_source_sha256"][owner]
        )

    mutations = []
    for name in payload["probe_scripts"]:
        mutations.append(lambda value, name=name: value["probe_scripts"].__setitem__(name, value["probe_scripts"][name] + "\n# changed"))
    mutations.extend(
        (
            lambda value: value.__setitem__("format_version", 3),
            lambda value: value["mandatory_check_ids"].reverse(),
            lambda value: value["mandatory_security_tests"].reverse(),
            lambda value: value["expected_fields"]["runtime"].pop(),
            lambda value: value["expected_behavior"][
                "mandatory_check_dependencies"
            ]["external_network_denied"].pop(),
            lambda value: value["expected_behavior"][
                "predicate_source_sha256"
            ].__setitem__("runtime_checks", "sha256:" + "0" * 64),
            lambda value: value["expected_behavior"][
                "predicate_source_sha256"
            ].__setitem__("case_evidence_validator", "sha256:" + "0" * 64),
            lambda value: value["expected_behavior"][
                "predicate_source_sha256"
            ].__setitem__("snapshot_source_tree", "sha256:" + "0" * 64),
            lambda value: value["expected_behavior"]["process_modes"][
                "timeout"
            ].__setitem__("timeout", 2),
            lambda value: value["expected_behavior"][
                "unsupported_entry_kinds"
            ].pop(),
            lambda value: value["external_fixtures"]["device"].__setitem__(
                "required", False
            ),
            lambda value: value["runtime_tool_vertical"].__setitem__(
                "shell_command", "true"
            ),
            lambda value: value.__setitem__("source_isolation_marker", "changed"),
            lambda value: value["behavior_parameters"].__setitem__(
                "output_probe_bytes", 1
            ),
        )
    )
    for mutation in mutations:
        changed = deepcopy(payload)
        mutation(changed)
        assert release._corpus_digest(changed) != release.CORPUS_DIGEST


@pytest.mark.parametrize(
    ("fields", "reader"),
    (
        (release._EPHEMERAL_PROBE_FIELDS, release._ephemeral_probe_facts),
        (release._PRIVILEGE_PROBE_FIELDS, release._privilege_probe_facts),
        (release._RESOURCE_PROBE_FIELDS, release._resource_probe_facts),
        (release._SENSITIVE_PROBE_FIELDS, release._sensitive_probe_facts),
        (release._TOOL_PROBE_FIELDS, release._tool_probe_facts),
        (release._WORKSPACE_CRUD_FIELDS, release._workspace_crud_facts),
        (
            release._WORKSPACE_PERSIST_FIELDS,
            release._workspace_persistence_facts,
        ),
    ),
)
def test_behavior_probe_readers_require_exact_clean_json_outcome(fields, reader):
    facts = {name: True for name in fields}
    raw = release._canonical_json(facts)

    assert reader(_outcome(stdout=raw, stdout_bytes=len(raw))) == facts
    assert reader(
        _outcome(stdout=raw, stdout_bytes=len(raw), stderr=b"warning", stderr_bytes=7)
    ) == {}
    assert reader(
        _outcome(stdout=raw, stdout_bytes=len(raw), cleanup_status="failed")
    ) == {}
    unexpected = release._canonical_json(dict(facts, unexpected=True))
    assert reader(_outcome(stdout=unexpected, stdout_bytes=len(unexpected))) == {}


def test_privilege_and_resource_checks_require_every_behavior():
    runtime = {name: True for name in release._RUNTIME_PROBE_FIELDS}
    privilege = {name: True for name in release._PRIVILEGE_PROBE_FIELDS}
    resources = {name: True for name in release._RESOURCE_PROBE_FIELDS}

    checks = release._runtime_probe_checks(
        runtime,
        host_listener_control=True,
        private_paths_absent=True,
        controlled_network_denied=True,
        privilege_facts=privilege,
        resource_facts=resources,
        oom_limited=True,
        disk_watchdog_limited=True,
        workspace_marker_exists=True,
        source_marker_exists=False,
    )
    assert checks["privilege_denied"] is True
    assert checks["readonly_rootfs"] is True
    assert checks["resource_limits"] is True

    for name in privilege:
        poisoned = dict(privilege, **{name: False})
        assert release._runtime_probe_checks(
            runtime,
            host_listener_control=True,
            private_paths_absent=True,
            controlled_network_denied=True,
            privilege_facts=poisoned,
            resource_facts=resources,
            oom_limited=True,
            disk_watchdog_limited=True,
            workspace_marker_exists=True,
            source_marker_exists=False,
        )["privilege_denied"] is False
    for name in resources:
        poisoned = dict(resources, **{name: False})
        assert release._runtime_probe_checks(
            runtime,
            host_listener_control=True,
            private_paths_absent=True,
            controlled_network_denied=True,
            privilege_facts=privilege,
            resource_facts=poisoned,
            oom_limited=True,
            disk_watchdog_limited=True,
            workspace_marker_exists=True,
            source_marker_exists=False,
        )["resource_limits"] is False
    for oom_limited, disk_limited in ((False, True), (True, False)):
        assert release._runtime_probe_checks(
            runtime,
            host_listener_control=True,
            private_paths_absent=True,
            controlled_network_denied=True,
            privilege_facts=privilege,
            resource_facts=resources,
            oom_limited=oom_limited,
            disk_watchdog_limited=disk_limited,
            workspace_marker_exists=True,
            source_marker_exists=False,
        )["resource_limits"] is False


def test_output_oom_and_disk_watchdog_outcomes_are_exact(tmp_path):
    output = _outcome(
        stdout=b"o" * release._OUTPUT_RETAINED_BYTES,
        stderr=b"e" * release._OUTPUT_RETAINED_BYTES,
        stdout_bytes=release._OUTPUT_PROBE_BYTES,
        stderr_bytes=release._OUTPUT_PROBE_BYTES,
        stdout_truncated=True,
        stderr_truncated=True,
    )
    assert release._output_probe_passed(output) is True
    assert release._output_probe_passed(
        _outcome(**{**vars(output), "stderr_bytes": release._OUTPUT_PROBE_BYTES - 1})
    ) is False
    assert release._output_probe_passed(
        _outcome(**{**vars(output), "target_started": False})
    ) is False

    oom = _outcome(
        sandbox_outcome="oom_killed",
        exit_code=137,
        error_code="sandbox_oom_killed",
    )
    assert release._oom_probe_passed(oom) is True
    assert release._oom_probe_passed(
        _outcome(
            sandbox_outcome="oom_killed",
            exit_code=137,
            error_code="sandbox_oom_killed",
            residue_detected=True,
        )
    ) is False

    overflow_paths = release._disk_watchdog_paths(tmp_path)
    for overflow in overflow_paths[:-1]:
        with overflow.open("xb") as target:
            target.truncate(release._DISK_WATCHDOG_FILE_BYTES)
    with overflow_paths[-1].open("xb") as target:
        target.truncate(1)
    watchdog = _outcome(
        sandbox_outcome="container_runtime_failed",
        exit_code=137,
        error_code="sandbox_workspace_limit_exceeded",
    )
    assert release._disk_watchdog_probe_passed(watchdog, overflow_paths) is True
    with overflow_paths[0].open("r+b") as target:
        target.truncate(release._DISK_WATCHDOG_FILE_BYTES - 1)
    assert release._disk_watchdog_probe_passed(watchdog, overflow_paths) is False


def test_host_sentinel_always_reaps_process_group(monkeypatch):
    events = []

    class Process:
        pid = 123

        def poll(self):
            return None

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired("sentinel", timeout)
            return 0

    monkeypatch.setattr(
        release.os,
        "killpg",
        lambda pid, sig: events.append(("killpg", pid, sig)),
    )

    release._stop_host_sentinel(Process())

    assert events == [
        ("killpg", 123, release.signal.SIGTERM),
        ("wait", release._HOST_SENTINEL_GRACE_SECONDS),
        ("killpg", 123, release.signal.SIGKILL),
        ("wait", None),
    ]


@pytest.mark.parametrize(
    ("expected_outcome", "overrides"),
    (
        ("completed", {}),
        (
            "timeout",
            {
                "sandbox_outcome": "timeout",
                "error_code": "sandbox_timeout",
                "timed_out": True,
            },
        ),
        (
            "interrupted",
            {
                "sandbox_outcome": "interrupted",
                "error_code": "sandbox_interrupted",
            },
        ),
    ),
)
def test_process_tree_predicate_requires_ready_and_no_heartbeats(
    tmp_path,
    expected_outcome,
    overrides,
):
    paths = release._process_tree_paths(tmp_path, "process")
    paths["ready"].write_bytes(b"ready\n")
    for path in paths["started"]:
        path.write_bytes(b"started\n")
    outcome = _outcome(**overrides)

    assert release._process_tree_probe_passed(
        outcome,
        paths,
        expected_outcome=expected_outcome,
    )
    paths["heartbeats"][0].write_bytes(b"residue\n")
    assert not release._process_tree_probe_passed(
        outcome,
        paths,
        expected_outcome=expected_outcome,
    )
    assert release._remove_probe_paths(paths) is True


def test_process_tree_control_requires_all_children_and_heartbeats(tmp_path):
    paths = release._process_tree_paths(tmp_path, "control")
    paths["ready"].write_bytes(b"ready\n")
    for path in (*paths["started"], *paths["heartbeats"]):
        path.write_bytes(b"control\n")

    assert release._process_tree_control_passed(_outcome(), paths) is True
    paths["started"][0].unlink()
    assert release._process_tree_control_passed(_outcome(), paths) is False
    assert release._remove_probe_paths(paths) is True


def test_interrupt_process_probe_cancels_helper_when_runner_raises(tmp_path):
    paths = release._process_tree_paths(tmp_path, "interrupt")

    class Runner:
        def execute(self, _session, _plan):
            raise RuntimeError("runner failed")

    context = SimpleNamespace(
        current_session=lambda: object(),
        runner=Runner(),
    )

    with pytest.raises(RuntimeError, match="runner failed"):
        release._interrupt_process_probe(context, object(), paths)

    paths["ready"].write_bytes(b"late ready\n")
    assert release._remove_probe_paths(paths) is True


def test_interrupt_process_probe_reads_verified_runner_interrupt_outcome(tmp_path):
    paths = release._process_tree_paths(tmp_path, "interrupt")
    outcome = _outcome(
        sandbox_outcome="interrupted",
        error_code="sandbox_interrupted",
    )

    class Runner:
        def execute(self, _session, _plan):
            paths["ready"].write_bytes(b"ready\n")
            try:
                while True:
                    time.sleep(0.01)
            except KeyboardInterrupt as error:
                error.docker_sandbox_outcome = outcome
                raise

    context = SimpleNamespace(
        current_session=lambda: object(),
        runner=Runner(),
    )

    observed, control = release._interrupt_process_probe(
        context,
        object(),
        paths,
    )

    assert observed is outcome
    assert control is True
    assert release._remove_probe_paths(paths) is True


@pytest.mark.parametrize("summary", ("1 skipped", "1 xfailed", "1 xpassed"))
def test_mandatory_security_pytest_rejects_nonpassing_outcomes(summary):
    assert release._pytest_passed(
        _outcome(stdout=("10 passed, " + summary).encode()),
        mandatory_security=True,
    ) is False


def test_mandatory_security_pytest_accepts_all_pass_and_full_suite_skips():
    outcome = _outcome(stdout=b"42 passed")

    assert release._pytest_passed(outcome, mandatory_security=True) is True
    assert release._pytest_passed(
        _outcome(stdout=b"42 passed, 2 skipped"),
        mandatory_security=False,
    ) is True
