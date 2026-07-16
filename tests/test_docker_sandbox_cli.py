import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import pico.cli.assembly as cli_assembly
import pico.cli.sandbox as cli_module
import pico.cli.app as pico_cli
from pico.state.checkpoint_store import CheckpointStore
from pico.cli.app import main
from pico.cli.errors import CliError
from pico.sandbox.docker import DockerSandboxError
from pico.sandbox.apply import StagingObserver
from pico.sandbox.apply import SandboxApplyError, SourceApplier, SourceApplyStore
from pico.sandbox.session import (
    read_source_apply_authority,
    SandboxSessionError,
    SandboxSessionStore,
)

EMPTY_CAPACITY = {
    "active_count": 0,
    "pending_count": 0,
    "cleanup_pending_count": 0,
    "staging_bytes": 0,
    "oldest_age_seconds": 0,
    "orphan_verified_count": 0,
    "orphan_unknown_count": 0,
    "reconciliation_required_count": 0,
}


def _session_metadata():
    return {
        "engine": {
            "endpoint_hash": "sha256:" + "1" * 64,
            "client_version": "29.5.2",
            "server_version": "29.5.2",
            "api_version": "1.54",
            "profile": "desktop_vm",
            "security_digest": "sha256:" + "2" * 64,
        },
        "image": {
            "image_digest": "sha256:" + "3" * 64,
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


def _bootstrap(request):
    git = request.workspace_view.physical_root / ".git"
    git.mkdir()
    (git / "HEAD").write_text(
        "ref: refs/heads/pico-sandbox\n",
        encoding="utf-8",
    )
    return "a" * 40


class _Context:
    def __init__(self, source, store, session):
        self.source_root = source
        self.execution_root = session.workspace_view.physical_root
        self.project_state_root = source / ".pico"
        self.sandbox_state_root = session.state_root
        self.source_apply_state_root = session.state_root
        self.sandbox_session = session
        self.runner = type("Runner", (), {"session_store": store})()

    def current_session(self):
        return self.runner.session_store.inspect(self.sandbox_state_root)


def _session(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("before\n", encoding="utf-8")
    store = SandboxSessionStore(home / ".pico" / "sandboxes")
    session = store.create(
        source,
        pico_session_id="session-1",
        bootstrap_git=_bootstrap,
        project_state_root=source / ".pico",
        **_session_metadata(),
    )
    context = _Context(source, store, session)
    checkpoint_store = CheckpointStore(
        session.state_root / "recovery" / ".pico" / "checkpoints"
    )
    observer = StagingObserver(context, checkpoint_store)
    observer.ensure_baseline()
    return home, source, context, observer


def test_status_is_read_only_and_reports_fixed_unavailable_reason(
    tmp_path,
    monkeypatch,
    capsys,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(
        cli_module,
        "discover_local_docker",
        lambda: (_ for _ in ()).throw(DockerSandboxError("docker_cli_unavailable")),
    )

    assert main(["--format", "json", "sandbox", "status"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "docker_sandbox_status"
    assert payload["data"]["status"] == "not_ready"
    assert payload["data"]["reason_code"] == "docker_cli_unavailable"
    assert payload["data"]["network_performed"] is False
    assert payload["data"]["mutation_performed"] is False
    assert payload["data"]["capacity"] == EMPTY_CAPACITY
    assert payload["data"]["runtime_authorization"] == {
        "status": "enabled",
        "kind": "local",
        "reason_code": "local_authorization_verified",
    }
    assert not (home / ".pico").exists()


def test_prepare_fails_closed_before_state_mutation_when_image_is_missing(
    tmp_path,
    monkeypatch,
    capsys,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    image = object()
    authorization = SimpleNamespace(attestation_kind="local")
    monkeypatch.setattr(
        cli_module,
        "local_docker_sandbox_runtime",
        lambda: (image, authorization),
    )
    monkeypatch.setattr(
        cli_module,
        "discover_local_docker",
        lambda: (Path("/docker"), Path("/docker.sock")),
    )

    class Client:
        def __init__(self, *_args):
            pass

        def prepare(self, _image):
            raise DockerSandboxError("sandbox_image_missing")

    monkeypatch.setattr(cli_module, "DockerClient", Client)

    assert main(["--format", "json", "sandbox", "prepare"]) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "sandbox_image_missing"
    assert not (home / ".pico").exists()


def test_prepare_only_inspects_packaged_local_image_without_network_or_cache(
    tmp_path,
    monkeypatch,
):
    events = []
    image = object()
    authorization = SimpleNamespace(attestation_kind="local")
    config = tmp_path / "packaged-config"
    monkeypatch.setattr(
        cli_module,
        "local_docker_sandbox_runtime",
        lambda: events.append("authorize") or (image, authorization),
    )
    monkeypatch.setattr(
        cli_module,
        "default_docker_config_path",
        lambda: config,
    )
    monkeypatch.setattr(
        cli_module,
        "discover_local_docker",
        lambda: events.append("discover") or (Path("/docker"), Path("/docker.sock")),
    )

    class Client:
        def __init__(self, cli, endpoint, selected_config):
            assert (cli, endpoint, selected_config) == (
                Path("/docker"),
                Path("/docker.sock"),
                config,
            )

        def prepare(self, selected_image):
            assert selected_image is image
            events.append("prepare")
            return {
                "status": "ready",
                "network_performed": False,
                "mutation_performed": False,
            }

    monkeypatch.setattr(cli_module, "DockerClient", Client)

    payload = cli_module._prepare_payload()

    assert events == ["authorize", "discover", "prepare"]
    assert payload["network_performed"] is False
    assert payload["mutation_performed"] is False
    assert payload["runtime_authorization"] == {
        "status": "enabled",
        "kind": "local",
        "reason_code": "local_authorization_verified",
    }


@pytest.mark.parametrize(
    "command",
    ("install", "repair", "export-bundle", "import-bundle"),
)
def test_removed_srt_lifecycle_commands_are_usage_errors(command, capsys):
    assert main(["--format", "json", "sandbox", command]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "usage"


def test_list_on_missing_root_is_read_only(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    assert main(["--format", "json", "sandbox", "list"]) == 0

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload == {"sessions": [], "capacity": EMPTY_CAPACITY}
    assert not (home / ".pico").exists()


def test_list_counts_unknown_state_without_disclosing_or_mutating_it(
    tmp_path,
    monkeypatch,
    capsys,
):
    home = tmp_path / "home"
    parent = home / ".pico" / "sandboxes"
    parent.mkdir(parents=True)
    parent.chmod(0o700)
    unknown = parent / "operator-note"
    unknown.write_text("do not trust\n", encoding="utf-8")
    before = unknown.stat()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    assert main(["--format", "json", "sandbox", "list"]) == 0

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload["sessions"] == []
    assert payload["capacity"] == {
        **EMPTY_CAPACITY,
        "orphan_unknown_count": 1,
    }
    assert "operator-note" not in json.dumps(payload)
    assert unknown.stat() == before


def test_status_and_list_map_invalid_state_root_to_fixed_errors(
    tmp_path,
    monkeypatch,
    capsys,
):
    home = tmp_path / "home"
    parent = home / ".pico" / "sandboxes"
    parent.mkdir(parents=True)
    parent.chmod(0o755)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    assert main(["--format", "json", "sandbox", "status"]) == 0

    status = json.loads(capsys.readouterr().out)["data"]
    assert status["status"] == "not_ready"
    assert status["reason_code"] == "sandbox_state_invalid"
    assert status["capacity"]["orphan_unknown_count"] == 1
    assert str(parent) not in json.dumps(status)

    assert main(["--format", "json", "sandbox", "list"]) == 3

    listed = json.loads(capsys.readouterr().out)
    assert listed["error"]["code"] == "sandbox_state_invalid"
    assert str(parent) not in json.dumps(listed)


def test_status_maps_packaged_manifest_failure_to_fixed_reason(
    tmp_path,
    monkeypatch,
    capsys,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(
        cli_module,
        "local_docker_sandbox_runtime",
        lambda: (_ for _ in ()).throw(
            DockerSandboxError("sandbox_image_identity_mismatch")
        ),
    )

    assert main(["--format", "json", "sandbox", "status"]) == 0

    status = json.loads(capsys.readouterr().out)["data"]
    assert status["status"] == "not_ready"
    assert status["reason_code"] == "sandbox_image_identity_mismatch"
    assert status["network_performed"] is False
    assert status["mutation_performed"] is False


def test_prune_refuses_all_mutation_when_unknown_state_exists(
    tmp_path,
    monkeypatch,
    capsys,
):
    home = tmp_path / "home"
    parent = home / ".pico" / "sandboxes"
    parent.mkdir(parents=True)
    parent.chmod(0o700)
    unknown = parent / "operator-note"
    unknown.write_text("do not trust\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    assert main(["--format", "json", "sandbox", "prune", "--apply"]) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "sandbox_prune_refused"
    assert unknown.read_text(encoding="utf-8") == "do not trust\n"


def test_list_inspect_diff_and_apply_use_production_artifacts(
    tmp_path,
    monkeypatch,
    capsys,
):
    _home, source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    finalized = observer.finalize_diff(lambda text: text)
    sandbox_id = context.sandbox_session.sandbox_id

    assert main(["--format", "json", "sandbox", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)["data"]["sessions"]
    assert listed[0]["sandbox_id"] == sandbox_id
    assert str(source) not in json.dumps(listed)

    assert main(["--format", "json", "sandbox", "list"]) == 0
    capacity = json.loads(capsys.readouterr().out)["data"]["capacity"]
    assert capacity["pending_count"] == 1
    assert capacity["staging_bytes"] > 0
    assert capacity["orphan_unknown_count"] == 0

    assert main(["--format", "json", "sandbox", "inspect", sandbox_id]) == 0
    inspected = json.loads(capsys.readouterr().out)["data"]
    assert inspected["state"] == "pending_review"
    assert str(context.execution_root) not in json.dumps(inspected)

    diff_path = context.sandbox_state_root / "recovery" / "diff.json"
    diff_before = diff_path.stat()
    assert main(["--format", "json", "sandbox", "diff", sandbox_id]) == 0
    diff = json.loads(capsys.readouterr().out)["data"]
    assert diff["diff_digest"] == finalized["diff_digest"]
    assert diff["entries"] == [
        {
            "change_kind": "modified",
            "classification": "candidate",
            "path": "README.md",
        }
    ]
    assert str(context.execution_root) not in json.dumps(diff)
    diff_after = diff_path.stat()
    assert (
        diff_after.st_dev,
        diff_after.st_ino,
        diff_after.st_mode,
        diff_after.st_size,
        diff_after.st_mtime_ns,
        diff_after.st_ctime_ns,
    ) == (
        diff_before.st_dev,
        diff_before.st_ino,
        diff_before.st_mode,
        diff_before.st_size,
        diff_before.st_mtime_ns,
        diff_before.st_ctime_ns,
    )

    assert main(["--format", "json", "sandbox", "apply", sandbox_id, "--yes"]) == 0
    captured = capsys.readouterr()
    applied = json.loads(captured.out)["data"]
    assert finalized["diff_digest"] in captured.err
    assert str(source) in captured.err
    assert "README.md" not in captured.err
    assert applied["status"] == "apply_applied"
    assert (source / "README.md").read_text(encoding="utf-8") == "after\n"
    assert not context.execution_root.exists()


def test_apply_requires_separate_confirmation(tmp_path, monkeypatch, capsys):
    _home, _source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    observer.finalize_diff(lambda text: text)

    assert (
        main(
        [
            "--format",
            "json",
            "--no-input",
            "sandbox",
            "apply",
            context.sandbox_session.sandbox_id,
        ]
        )
        == 2
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "confirmation_required"


def test_apply_review_summarizes_exact_artifact_and_risky_paths(
    tmp_path,
    monkeypatch,
):
    _home, source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    script = context.execution_root / "scripts" / "run.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    (context.execution_root / ".env").write_text("TOKEN=value\n", encoding="utf-8")
    finalized = observer.finalize_diff(lambda text: text)

    review = cli_module._apply_review(context.current_session())

    assert review == {
        "sandbox_id": context.sandbox_session.sandbox_id,
        "diff_digest": finalized["diff_digest"],
        "source_root": str(source),
        "candidate_count": 2,
        "candidate_bytes": len(b"after\n") + len(b"#!/bin/sh\n"),
        "change_counts": {
            "created": 2,
            "modified": 1,
            "deleted": 0,
            "type_changed": 0,
        },
        "high_risk_count": 1,
        "blocked_count": 1,
        "high_risk_or_blocked_paths": [
            {"path": ".env", "classification": "blocked_sensitive"},
            {"path": "scripts/run.sh", "classification": "high_risk_candidate"},
        ],
    }


def test_apply_yes_loads_review_and_passes_displayed_digest(
    tmp_path,
    monkeypatch,
    capsys,
):
    _home, _source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    finalized = observer.finalize_diff(lambda text: text)
    received = []

    def fake_apply(_store, _session, digest):
        received.append(digest)
        return {"status": "bound"}

    monkeypatch.setattr(cli_module, "_apply", fake_apply)

    assert (
        main(
        [
            "--format",
            "json",
            "sandbox",
            "apply",
            context.sandbox_session.sandbox_id,
            "--yes",
        ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert received == [finalized["diff_digest"]]
    assert finalized["diff_digest"] in captured.err
    assert json.loads(captured.out)["data"]["status"] == "bound"


def test_apply_displays_loaded_review_before_interactive_confirmation(
    tmp_path,
    monkeypatch,
    capsys,
):
    _home, _source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    finalized = observer.finalize_diff(lambda text: text)
    events = []
    monkeypatch.setattr(
        cli_module,
        "_display_apply_review",
        lambda review: events.append(("display", review["diff_digest"])),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: events.append(("confirm", "")) or "n",
    )

    assert (
        main(
        ["--format", "json", "sandbox", "apply", context.sandbox_session.sandbox_id]
        )
        == 2
    )

    assert events == [("display", finalized["diff_digest"]), ("confirm", "")]
    assert json.loads(capsys.readouterr().out)["error"]["code"] == (
        "confirmation_declined"
    )


def test_discard_removes_staging_and_preserves_terminal_audit(
    tmp_path,
    monkeypatch,
    capsys,
):
    _home, source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "candidate.txt").write_text(
        "candidate\n",
        encoding="utf-8",
    )
    observer.finalize_diff(lambda text: text)
    sandbox_id = context.sandbox_session.sandbox_id

    assert main(["--format", "json", "sandbox", "discard", sandbox_id, "--yes"]) == 0

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload["state"] == "discarded"
    assert not context.execution_root.exists()
    assert context.sandbox_state_root.exists()
    assert not (source / "candidate.txt").exists()


def test_discard_cannot_hide_pre_session_apply_crash(
    tmp_path,
    monkeypatch,
    capsys,
):
    _home, source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_guard(stage, _path):
        if stage == "after_guard":
            raise KeyboardInterrupt("crash")

    with pytest.raises(KeyboardInterrupt, match="crash"):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_guard,
        ).apply(diff["diff_digest"])
    sandbox_id = context.sandbox_session.sandbox_id

    assert main(["--format", "json", "sandbox", "discard", sandbox_id, "--yes"]) == 3

    refused = json.loads(capsys.readouterr().out)
    assert refused["error"]["code"] == "source_apply_review_required"
    assert context.current_session().state == "pending_review"
    assert context.execution_root.is_dir()
    assert (
        read_source_apply_authority(
        context.runner.session_store.parent,
        source,
        )
        is not None
    )

    assert main(["--format", "json", "sandbox", "apply", sandbox_id, "--yes"]) == 0
    recovered = json.loads(capsys.readouterr().out)["data"]
    assert recovered["status"] == "apply_failed_rolled_back"
    assert (
        read_source_apply_authority(
        context.runner.session_store.parent,
        source,
        )
        is None
    )


def test_reconcile_uses_lexical_authority_after_source_root_replacement(
    tmp_path,
    monkeypatch,
    capsys,
):
    _home, source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)
    detached = tmp_path / "detached-source"

    def replace_source_and_crash(stage, _path):
        if stage == "after_journal":
            source.rename(detached)
            source.mkdir()
            (source / "README.md").write_text("replacement\n", encoding="utf-8")
            raise KeyboardInterrupt("crash")

    with pytest.raises(KeyboardInterrupt, match="crash"):
        SourceApplier(
            context,
            observer,
            fault_injector=replace_source_and_crash,
        ).apply(diff["diff_digest"])

    monkeypatch.setattr(
        cli_module,
        "_inventory",
        lambda *_args: pytest.fail("global inventory reached"),
    )
    monkeypatch.setattr(
        cli_module,
        "_find",
        lambda *_args: pytest.fail("global find reached"),
    )

    assert (
        main(
        [
            "--format",
            "json",
            "--cwd",
            str(source),
            "sandbox",
            "reconcile",
            "--yes",
        ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload == {
        "sandbox_id": context.sandbox_session.sandbox_id,
        "state": "review_required",
        "apply_status": "apply_review_required",
        "journal_id": payload["journal_id"],
        "journal_status": "apply_review_required",
    }
    manifest = json.loads(
        (context.sandbox_state_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["state"] == "review_required"
    assert manifest["apply"] == {
        "journal_id": payload["journal_id"],
        "status": "apply_review_required",
    }
    assert (source / "README.md").read_text(encoding="utf-8") == "replacement\n"
    assert (detached / "README.md").read_text(encoding="utf-8") == "before\n"


def test_reconcile_requires_explicit_confirmation(tmp_path, monkeypatch, capsys):
    _home, source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_journal(stage, _path):
        if stage == "after_journal":
            raise KeyboardInterrupt("crash")

    with pytest.raises(KeyboardInterrupt, match="crash"):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_journal,
        ).apply(diff["diff_digest"])
    journal_id = context.current_session().manifest["apply"]["journal_id"]
    before = SourceApplyStore(context.sandbox_state_root).load_journal(journal_id)

    assert (
        main(
        [
            "--format",
            "json",
            "--no-input",
            "--cwd",
            str(source),
            "sandbox",
            "reconcile",
        ]
        )
        == 2
    )

    assert json.loads(capsys.readouterr().out)["error"]["code"] == (
        "confirmation_required"
    )
    assert (
        SourceApplyStore(context.sandbox_state_root).load_journal(journal_id) == before
    )
    assert context.current_session().state == "applying"


def test_reconcile_fails_closed_on_authority_journal_mismatch(
    tmp_path,
    monkeypatch,
    capsys,
):
    _home, source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def crash_after_journal(stage, _path):
        if stage == "after_journal":
            raise KeyboardInterrupt("crash")

    with pytest.raises(KeyboardInterrupt, match="crash"):
        SourceApplier(
            context,
            observer,
            fault_injector=crash_after_journal,
        ).apply(diff["diff_digest"])
    authority = read_source_apply_authority(
        context.runner.session_store.parent,
        source,
    )
    path = SourceApplyStore(context.sandbox_state_root).journals / (
        f"{authority['journal_id']}.json"
    )
    journal = json.loads(path.read_text(encoding="utf-8"))
    journal["diff_digest"] = "sha256:" + "f" * 64
    path.write_text(json.dumps(journal), encoding="utf-8")
    path.chmod(0o600)

    assert (
        main(
        [
            "--format",
            "json",
            "--cwd",
            str(source),
            "sandbox",
            "reconcile",
            "--yes",
        ]
        )
        == 3
    )

    assert json.loads(capsys.readouterr().out)["error"]["code"] == (
        "sandbox_apply_journal_invalid"
    )
    assert context.current_session().state == "applying"


def test_public_sandbox_runtime_fails_closed_on_local_authorization_before_agent_build(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr("pico.cli.app.platform.system", lambda: "Darwin")
    monkeypatch.setattr("pico.cli.app.platform.machine", lambda: "arm64")
    monkeypatch.setattr(
        "pico.cli.assembly._build_transport_client",
        lambda *_args, **_kwargs: pytest.fail("provider construction reached"),
    )
    monkeypatch.setattr(
        "pico.cli.assembly.local_docker_sandbox_runtime",
        lambda: (_ for _ in ()).throw(
            DockerSandboxError("sandbox_runtime_authorization_mismatch")
        ),
    )

    assert (
        main(["--format", "json", "--cwd", str(tmp_path), "--sandbox", "run", "hello"])
        == 3
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "sandbox_runtime_authorization_mismatch"


@pytest.mark.parametrize(
    ("system", "machine"),
    (("Linux", "arm64"), ("Darwin", "x86_64"), ("Windows", "AMD64")),
)
def test_public_sandbox_runtime_rejects_unreleased_local_platform_before_build(
    tmp_path,
    monkeypatch,
    capsys,
    system,
    machine,
):
    monkeypatch.setattr("pico.cli.app.platform.system", lambda: system)
    monkeypatch.setattr("pico.cli.app.platform.machine", lambda: machine)
    monkeypatch.setattr(
        "pico.cli.app.build_agent",
        lambda *_args, **_kwargs: pytest.fail("agent construction reached"),
    )

    assert (
        main(["--format", "json", "--cwd", str(tmp_path), "--sandbox", "run", "hello"])
        == 3
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "sandbox_local_platform_not_released"


def test_sandbox_preflight_failure_precedes_source_session_store(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "source-secret")
    monkeypatch.setattr(
        cli_assembly,
        "_load_sandbox_runtime",
        lambda: (object(), object()),
    )
    monkeypatch.setattr(
        cli_assembly,
        "_build_sandbox_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CliError(
                code="sandbox_image_missing",
                message="Docker Sandbox startup failed",
                exit_code=3,
            )
        ),
    )
    monkeypatch.setattr(
        cli_assembly,
        "_build_transport_client",
        lambda *_args, **_kwargs: pytest.fail("provider construction reached"),
    )
    args = pico_cli.build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--sandbox", "run", "hello"]
    )

    with pytest.raises(CliError, match="Docker Sandbox startup failed"):
        cli_assembly.build_agent(args)

    assert not (tmp_path / ".pico").exists()


def test_runtime_defaults_to_fresh_sealed_local_authorization(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    image = object()
    authorization = object()
    calls = []
    monkeypatch.setattr(
        cli_assembly,
        "local_docker_sandbox_runtime",
        lambda: calls.append("local") or (image, authorization),
    )

    assert cli_assembly._load_sandbox_runtime() == (image, authorization)
    assert calls == ["local"]
    assert not (home / ".pico").exists()


def test_build_agent_wires_verified_runtime_to_one_docker_context(
    tmp_path,
    monkeypatch,
):
    events = []
    image = object()
    authorization = object()
    context = object()
    model = object()
    captured = {}

    class FakePico:
        @staticmethod
        def new_session_id():
            return "session-1"

        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("OPENAI_API_KEY", "source-secret")
    monkeypatch.setattr(
        cli_assembly,
        "_load_sandbox_runtime",
        lambda: events.append("runtime") or (image, authorization),
    )
    monkeypatch.setattr(
        cli_assembly,
        "_build_transport_client",
        lambda *_args, **_kwargs: events.append("model") or model,
    )

    def build_context(source_workspace, selected, **kwargs):
        events.append("context")
        assert selected is image
        assert kwargs["authorization"] is authorization
        assert kwargs["pico_session_id"] == "session-1"
        assert kwargs["resume"] is False
        assert b"source-secret" in kwargs["known_secrets"]
        return context, source_workspace

    monkeypatch.setattr(cli_assembly, "_build_sandbox_context", build_context)
    monkeypatch.setattr(cli_assembly, "Pico", FakePico)
    args = pico_cli.build_arg_parser().parse_args(
        ["--cwd", str(tmp_path), "--sandbox", "run", "hello"]
    )

    agent = cli_assembly.build_agent(args)

    assert isinstance(agent, FakePico)
    assert events == ["runtime", "context", "model"]
    assert captured["model_client"] is model
    assert captured["options"].sandbox_context is context
    assert captured["options"].session_id == "session-1"


def test_host_resume_rejects_bound_sandbox_before_model_construction(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    events = []
    monkeypatch.setattr(
        cli_assembly,
        "find_project_sandbox_session",
        lambda project_state_root, source_root, pico_session_id: (
            events.append(
                (
                    "finder",
                    project_state_root,
                    source_root,
                    pico_session_id,
                )
            )
            or object()
        ),
    )
    monkeypatch.setattr(
        cli_assembly,
        "_build_transport_client",
        lambda *_args, **_kwargs: pytest.fail("model constructed"),
    )
    args = pico_cli.build_arg_parser().parse_args(
        [
            "--cwd",
            str(tmp_path),
            "--resume",
            "session-1",
            "run",
            "hello",
        ]
    )

    with pytest.raises(CliError) as caught:
        cli_assembly.build_agent(args)

    assert caught.value.code == "sandbox_session_mode_mismatch"
    assert events == [
        (
            "finder",
            tmp_path / ".pico",
            tmp_path,
            "session-1",
        )
    ]


def test_host_resume_fails_closed_on_invalid_sandbox_binding(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")

    def invalid_binding(*_args):
        raise SandboxSessionError("sandbox_manifest_invalid")

    monkeypatch.setattr(
        cli_assembly,
        "find_project_sandbox_session",
        invalid_binding,
    )
    monkeypatch.setattr(
        cli_assembly,
        "_build_transport_client",
        lambda *_args, **_kwargs: pytest.fail("model constructed"),
    )
    args = pico_cli.build_arg_parser().parse_args(
        [
            "--cwd",
            str(tmp_path),
            "--resume",
            "session-1",
            "run",
            "hello",
        ]
    )

    with pytest.raises(CliError) as caught:
        cli_assembly.build_agent(args)

    assert caught.value.code == "sandbox_state_invalid"


def test_prune_dry_run_is_read_only_and_apply_failure_releases_lease(
    tmp_path,
    monkeypatch,
    capsys,
):
    _home, _source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "candidate.txt").write_text(
        "candidate\n",
        encoding="utf-8",
    )
    observer.finalize_diff(lambda text: text)
    store = context.runner.session_store
    current = context.current_session()
    store.release(current.state_root, current.manifest["lease"]["owner_nonce"])
    manifest_path = current.state_root / "manifest.json"
    before = manifest_path.read_bytes()
    monkeypatch.setattr(cli_module, "_age_seconds", lambda _timestamp: 10**9)

    assert main(["--format", "json", "sandbox", "prune", "--dry-run"]) == 0

    dry_run = json.loads(capsys.readouterr().out)["data"]
    assert dry_run["candidate_count"] == 1
    assert manifest_path.read_bytes() == before

    def fail_discard(_self, _state_root):
        raise SandboxSessionError("injected_cleanup_failure")

    monkeypatch.setattr(SandboxSessionStore, "discard", fail_discard)
    assert main(["--format", "json", "sandbox", "prune", "--apply"]) == 0

    applied = json.loads(capsys.readouterr().out)["data"]
    assert applied["outcomes"] == [
        {
            "sandbox_id": current.sandbox_id,
            "status": "injected_cleanup_failure",
        }
    ]
    assert context.current_session().manifest["lease"] is None


def test_prune_apply_resumes_bounded_cleanup_pending_session(
    tmp_path,
    monkeypatch,
    capsys,
):
    _home, _source, context, _observer = _session(tmp_path, monkeypatch)
    store = context.runner.session_store
    pending = store.discard(
        context.sandbox_state_root,
        max_delete_entries=1,
    )
    assert pending.state == "cleanup_pending"

    assert main(["--format", "json", "sandbox", "prune", "--apply"]) == 0

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload["outcomes"] == [
        {"sandbox_id": pending.sandbox_id, "status": "cleaned"}
    ]
    current = context.current_session()
    assert current.state == "discarded"
    assert current.manifest["cleanup"]["status"] == "complete"


def test_prune_apply_reconciles_active_call_before_staging_candidates(
    tmp_path,
    monkeypatch,
    capsys,
):
    _home, _source, context, _observer = _session(tmp_path, monkeypatch)
    store = context.runner.session_store
    current = context.current_session()
    store.begin_call(
        current.state_root,
        call_id="call-1",
        reconciliation_token="c" * 64,
        container_name="pico-call-1",
        expected_labels={"io.pico.call": "call-1"},
        plan_digest="sha256:" + "d" * 64,
    )

    class Reconciler:
        def reconcile_session(self, acquired):
            assert acquired.manifest["active_call"] is not None
            return store.finish_call(acquired.state_root)

    monkeypatch.setattr(
        cli_module,
        "_reconciliation_runner",
        lambda active_store: (
            Reconciler() if active_store.parent == store.parent else None
        ),
    )

    assert main(["--format", "json", "sandbox", "prune", "--apply"]) == 0

    payload = json.loads(capsys.readouterr().out)["data"]
    assert payload["outcomes"] == [
        {
            "sandbox_id": current.sandbox_id,
            "state": "ready",
            "status": "reconciled",
        }
    ]
    assert payload["reconciliation_required_count"] == 0
    assert context.current_session().manifest["active_call"] is None


def test_prune_retries_source_apply_blob_cleanup_before_workspace_cleanup(
    tmp_path,
    monkeypatch,
    capsys,
):
    _home, _source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    observer.finalize_diff(lambda text: text)
    original = SourceApplyStore.cleanup_terminal_blobs
    monkeypatch.setattr(
        SourceApplyStore,
        "cleanup_terminal_blobs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SandboxApplyError("sandbox_apply_cleanup_failed")
        ),
    )

    assert (
        main(
        [
            "--format",
            "json",
            "sandbox",
            "apply",
            context.sandbox_session.sandbox_id,
            "--yes",
        ]
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)["data"]
    assert applied["status"] == "applied_cleanup_pending"
    apply_store = SourceApplyStore(context.source_apply_state_root)
    journal = apply_store.load_journal(applied["journal_id"])
    blob_ref = journal["entries"][0]["before_blob_ref"]
    blob_path = apply_store.blobs / blob_ref[:2] / blob_ref
    assert blob_path.is_file()
    assert context.execution_root.is_dir()
    assert CheckpointStore(context.source_root).source_apply_guard() is not None
    assert (
        read_source_apply_authority(
        context.runner.session_store.parent,
        context.source_root,
        )
        is not None
    )
    monkeypatch.setattr(SourceApplyStore, "cleanup_terminal_blobs", original)

    assert main(["--format", "json", "sandbox", "prune", "--apply"]) == 0

    pruned = json.loads(capsys.readouterr().out)["data"]
    assert pruned["outcomes"] == [
        {"sandbox_id": context.sandbox_session.sandbox_id, "status": "cleaned"}
    ]
    assert not blob_path.exists()
    assert not context.execution_root.exists()
    assert CheckpointStore(context.source_root).source_apply_guard() is None
    assert (
        read_source_apply_authority(
        context.runner.session_store.parent,
        context.source_root,
        )
        is None
    )


def test_discard_cleans_rolled_back_source_apply_blobs(tmp_path, monkeypatch, capsys):
    _home, source, context, observer = _session(tmp_path, monkeypatch)
    (context.execution_root / "README.md").write_text("after\n", encoding="utf-8")
    diff = observer.finalize_diff(lambda text: text)

    def fail_after_mutation(stage, _path):
        if stage == "after_mutation":
            raise OSError("injected")

    result = SourceApplier(
        context,
        observer,
        fault_injector=fail_after_mutation,
    ).apply(diff["diff_digest"])
    assert result["status"] == "apply_failed_rolled_back"
    apply_store = SourceApplyStore(context.source_apply_state_root)
    journal = apply_store.load_journal(result["journal_id"])
    blob_ref = journal["entries"][0]["before_blob_ref"]
    blob_path = apply_store.blobs / blob_ref[:2] / blob_ref
    assert not blob_path.exists()
    assert (source / "README.md").read_text(encoding="utf-8") == "before\n"

    assert (
        main(
        [
            "--format",
            "json",
            "sandbox",
            "discard",
            context.sandbox_session.sandbox_id,
            "--yes",
        ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out)["data"]["state"] == "discarded"
    assert not blob_path.exists()
