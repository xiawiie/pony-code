from dataclasses import replace
from pathlib import Path
import shutil
from types import MappingProxyType

import pytest

import pico.docker_sandbox as docker_module
import pico.sandbox_release_authority as release_authority
import pico.tool_executor as tool_executor_module
import pico.workspace as workspace_module
from pico.checkpoint_store import CheckpointStore
from pico.config import load_pico_toml
from pico.docker_sandbox import (
    build_docker_sandbox_context,
    compile_execution_plan,
    DockerExecutionOutcome,
    DockerSandboxError,
)
from pico.providers.fake import FakeModelClient
from pico.runtime import Pico
from pico.sandbox_session import snapshot_source_tree, write_source_apply_authority
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


CLIENT_DIGEST = "sha256:" + "d" * 64


class FakeDockerClient:
    fail_readiness = False

    def __init__(self, cli, endpoint, config):
        self.cli = cli
        self.endpoint = endpoint
        self.config = config

    def identity_digest(self):
        return CLIENT_DIGEST

    def require_ready(self, image):
        del image
        if self.fail_readiness:
            raise DockerSandboxError("docker_daemon_unavailable")
        return {
            "record_type": "docker_sandbox_status",
            "format_version": 1,
            "status": "ready",
            "reason_code": "ready",
            "platform_profile": "desktop_vm",
            "client_version": "29.5.2",
            "server_version": "29.5.2",
            "api_version": "1.54",
            "security": {
                "rootless": False,
                "seccomp": "builtin",
                "cgroup_limits": True,
                "eci": "unknown",
            },
        }


class FakeDockerRunner:
    cleanup_failure = False
    interrupt = None

    def __init__(self, client, session_store, image):
        self.client = client
        self.session_store = session_store
        self.image = image
        self.targets = []

    def bootstrap_git(self, request):
        git = request.workspace_view.physical_root / ".git"
        git.mkdir()
        (git / "HEAD").write_text(
            "ref: refs/heads/pico-sandbox\n",
            encoding="utf-8",
        )
        return "a" * 40

    def compile(self, session, target_argv, *, timeout, logical_intent_digest=None):
        return compile_execution_plan(
            session,
            self.image,
            CLIENT_DIGEST,
            target_argv,
            timeout=timeout,
            logical_intent_digest=logical_intent_digest,
        )

    def execute(self, session, plan):
        plan.verify()
        self.targets.append(plan.target_argv)
        workspace = session.workspace_view.physical_root
        stdout = b""
        if plan.target_argv[:2] == (self.image.tool_map["shell"], "-c"):
            command = plan.target_argv[2]
            if command == "cat shared.txt":
                stdout = (workspace / "shared.txt").read_bytes()
            elif command == "printf 'from-shell\\n' > shell.txt":
                (workspace / "shell.txt").write_text(
                    "from-shell\n",
                    encoding="utf-8",
                )
            elif command == "printf 'changed\\n' > README.md":
                (workspace / "README.md").write_text(
                    "changed\n",
                    encoding="utf-8",
                )
        outcome = DockerExecutionOutcome(
            stdout=stdout,
            stderr=b"",
            stdout_bytes=len(stdout),
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            exit_code=0,
            timed_out=False,
            runner_executed=True,
            target_started=True,
            container_created=True,
            sandbox_outcome=("interrupted" if self.interrupt else "completed"),
            cleanup_status="failed" if self.cleanup_failure else "completed",
            residue_detected=self.cleanup_failure,
            error_code=(
                "container_cleanup_failed"
                if self.cleanup_failure
                else "sandbox_interrupted"
                if self.interrupt
                else ""
            ),
        )
        if self.interrupt is not None:
            self.interrupt.docker_sandbox_outcome = outcome
            raise self.interrupt
        return outcome


def _install_fake_docker(monkeypatch):
    FakeDockerClient.fail_readiness = False
    FakeDockerRunner.cleanup_failure = False
    FakeDockerRunner.interrupt = None
    monkeypatch.setattr(docker_module, "DockerClient", FakeDockerClient)
    monkeypatch.setattr(docker_module, "DockerSandboxRunner", FakeDockerRunner)


def _development_authorization(monkeypatch, image):
    monkeypatch.setattr(
        release_authority,
        "installed_tree_digest",
        lambda _root, _version=None: "sha256:" + "9" * 64,
    )
    return docker_module._authorize_docker_sandbox_development(
        package_root=Path(docker_module.__file__).resolve().parent,
        distribution_version="0.1.0",
        image=image,
    )


def _build_runtime(
    tmp_path,
    monkeypatch,
    *,
    source_status="(unavailable)",
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _install_fake_docker(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("source\n", encoding="utf-8")
    (source / ".env").write_text(
        "OPENAI_API_KEY=source-secret\n",
        encoding="utf-8",
    )
    project_config = load_pico_toml(source)
    source_workspace = WorkspaceContext.build(source)
    image = docker_module.load_image_manifest(
        docker_module.default_image_manifest_path()
    )
    authorization = _development_authorization(monkeypatch, image)
    context = build_docker_sandbox_context(
        source,
        authorization=authorization,
        pico_session_id="session-1",
        docker_cli="/unused/docker",
        docker_endpoint="/unused/docker.sock",
        project_state_root=source / ".pico",
        sandbox_parent=tmp_path / "sandboxes",
        git_executable=None,
        known_secrets=(b"source-secret",),
        image=image,
        source_branch="source-only-branch",
        source_status=source_status,
        source_default_branch="source-only-default",
    )
    executables = {
        name: path
        for name, path in source_workspace.trusted_executables.items()
        if name != "git"
    }
    workspace = WorkspaceContext.build(
        context.execution_root,
        repo_root_override=context.execution_root,
        executables=executables,
        inspect_git=False,
        logical_root=context.logical_root,
        branch_override="pico-sandbox",
        default_branch_override="pico-sandbox",
        status_override="clean",
    )
    redaction_env = MappingProxyType({"OPENAI_API_KEY": "source-secret"})
    store = SessionStore(source / ".pico" / "sessions")
    agent = Pico._for_docker_sandbox_development(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="ask",
        redaction_env=redaction_env,
        _trusted_redaction_env=True,
        sandbox_context=context,
        project_config=project_config,
        session_id="session-1",
    )
    agent.approve = lambda _name, _args: True
    return source, context, agent


def test_context_requires_exact_sealed_runtime_authorization_before_readiness(
    tmp_path,
    monkeypatch,
):
    _install_fake_docker(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    image = docker_module.load_image_manifest(
        docker_module.default_image_manifest_path()
    )
    authorization = _development_authorization(monkeypatch, image)
    wrong = replace(authorization, corpus_digest="sha256:" + "0" * 64)

    with pytest.raises(TypeError, match="authorization"):
        build_docker_sandbox_context(
            source,
            pico_session_id="missing-authorization",
            docker_cli="/unused/docker",
            docker_endpoint="/unused/docker.sock",
            image=image,
        )
    with pytest.raises(DockerSandboxError, match="sandbox_runtime_authorization_mismatch"):
        build_docker_sandbox_context(
            source,
            authorization=wrong,
            pico_session_id="wrong-authorization",
            docker_cli="/unused/docker",
            docker_endpoint="/unused/docker.sock",
            image=image,
        )

    assert not any((tmp_path / "sandboxes").glob("*/sandbox_*"))


def test_public_pico_rejects_development_authorization(tmp_path, monkeypatch):
    source, context, agent = _build_runtime(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="product or candidate authorization"):
        Pico(
            model_client=FakeModelClient([]),
            workspace=agent.workspace,
            session_store=SessionStore(source / ".pico" / "other-sessions"),
            redaction_env=agent.redaction_env,
            _trusted_redaction_env=True,
            sandbox_context=context,
            project_config=agent.project_config,
            session_id="session-1",
        )


def test_local_runtime_authorization_is_packaged_and_rechecks_tree(monkeypatch):
    current = {"digest": "sha256:" + "9" * 64}
    monkeypatch.setattr(
        release_authority,
        "installed_tree_digest",
        lambda _root, _version=None: current["digest"],
    )

    image, authorization = docker_module.local_docker_sandbox_runtime()

    assert image == docker_module.load_image_manifest(
        docker_module.default_image_manifest_path()
    )
    assert authorization.attestation_kind == "local"
    assert authorization.release_sequence == 0
    assert authorization.installed_tree_digest == current["digest"]
    assert authorization.verify(image) is authorization

    current["digest"] = "sha256:" + "8" * 64
    fresh_image, fresh_authorization = docker_module.local_docker_sandbox_runtime()

    assert fresh_image == image
    assert fresh_authorization is not authorization
    assert fresh_authorization.installed_tree_digest == current["digest"]
    assert fresh_authorization.verify(fresh_image) is fresh_authorization
    with pytest.raises(
        DockerSandboxError,
        match="sandbox_runtime_authorization_mismatch",
    ):
        authorization.verify(image)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("image_set_digest", "sha256:" + "0" * 64),
        ("reference", "sha256:" + "0" * 64),
        ("image_id", "sha256:" + "0" * 64),
        ("platform", "linux/amd64"),
        ("policy_digest", "sha256:" + "0" * 64),
        ("corpus_digest", "sha256:" + "0" * 64),
    ),
)
def test_local_runtime_authorization_binds_exact_packaged_image(
    monkeypatch,
    field,
    value,
):
    monkeypatch.setattr(
        release_authority,
        "installed_tree_digest",
        lambda _root, _version=None: "sha256:" + "9" * 64,
    )
    image, authorization = docker_module.local_docker_sandbox_runtime()

    with pytest.raises(
        DockerSandboxError,
        match="sandbox_runtime_authorization_mismatch",
    ):
        authorization.verify(replace(image, **{field: value}))


def test_public_pico_accepts_local_authorization(tmp_path, monkeypatch):
    source, context, agent = _build_runtime(tmp_path, monkeypatch)
    _image, authorization = docker_module.local_docker_sandbox_runtime()
    local_context = replace(context, authorization=authorization)

    local_agent = Pico(
        model_client=FakeModelClient([]),
        workspace=agent.workspace,
        session_store=SessionStore(source / ".pico" / "local-sessions"),
        redaction_env=agent.redaction_env,
        _trusted_redaction_env=True,
        sandbox_context=local_context,
        project_config=agent.project_config,
        session_id="session-1",
        depth=1,
    )

    assert local_agent.sandbox_context.authorization.attestation_kind == "local"


def test_runtime_context_persists_full_engine_image_and_policy_identity(
    tmp_path,
    monkeypatch,
):
    _source, context, _agent = _build_runtime(tmp_path, monkeypatch)
    manifest = context.current_session().manifest

    assert set(manifest["engine"]) == {
        "endpoint_hash",
        "client_version",
        "server_version",
        "api_version",
        "profile",
        "security_digest",
    }
    assert manifest["engine"]["endpoint_hash"] == CLIENT_DIGEST
    assert manifest["engine"]["profile"] == "desktop_vm"
    assert set(manifest["image"]) == {
        "reference",
        "manifest_digest",
        "image_id",
        "platform",
    }
    assert manifest["image"]["manifest_digest"].startswith("sha256:")
    assert manifest["image"]["platform"] == "linux/arm64"
    assert set(manifest["policy"]) == {
        "version",
        "digest",
        "network",
        "mount_digest",
        "resource_digest",
    }
    assert manifest["policy"]["network"] == "none"


def test_readiness_failure_precedes_staging_and_project_sidecar(
    tmp_path,
    monkeypatch,
):
    _install_fake_docker(monkeypatch)
    FakeDockerClient.fail_readiness = True
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("source\n", encoding="utf-8")
    image = docker_module.load_image_manifest(
        docker_module.default_image_manifest_path()
    )
    authorization = _development_authorization(monkeypatch, image)

    with pytest.raises(DockerSandboxError, match="docker_daemon_unavailable"):
        build_docker_sandbox_context(
            source,
            authorization=authorization,
            pico_session_id="session-1",
            docker_cli="/unused/docker",
            docker_endpoint="/unused/docker.sock",
            project_state_root=source / ".pico",
            sandbox_parent=tmp_path / "sandboxes",
            image=image,
        )

    assert not any((tmp_path / "sandboxes").glob("*/sandbox_*"))
    assert not (source / ".pico").exists()


def test_external_project_state_build_leaves_source_tree_unchanged(
    tmp_path,
    monkeypatch,
):
    _install_fake_docker(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("source\n", encoding="utf-8")
    before = snapshot_source_tree(source)
    image = docker_module.load_image_manifest(
        docker_module.default_image_manifest_path()
    )
    authorization = _development_authorization(monkeypatch, image)

    context = build_docker_sandbox_context(
        source,
        authorization=authorization,
        pico_session_id="session-external-state",
        docker_cli="/unused/docker",
        docker_endpoint="/unused/docker.sock",
        project_state_root=tmp_path / "project-state",
        sandbox_parent=tmp_path / "sandboxes",
        image=image,
    )

    assert context.project_state_root == tmp_path / "project-state"
    assert snapshot_source_tree(source) == before
    assert not (source / ".pico").exists()


def test_unresolved_source_apply_guard_blocks_new_sandbox_before_staging(
    tmp_path,
    monkeypatch,
):
    _install_fake_docker(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("source\n", encoding="utf-8")
    checkpoint_store = CheckpointStore(source)
    with checkpoint_store.mutation_lock():
        checkpoint_store.begin_source_apply_guard(
            journal_id="apply_" + "1" * 32,
            sandbox_id="sandbox_" + "2" * 32,
            diff_digest="sha256:" + "3" * 64,
        )
    image = docker_module.load_image_manifest(
        docker_module.default_image_manifest_path()
    )
    authorization = _development_authorization(monkeypatch, image)

    with pytest.raises(DockerSandboxError, match="source_apply_review_required"):
        build_docker_sandbox_context(
            source,
            authorization=authorization,
            pico_session_id="session-guarded",
            docker_cli="/unused/docker",
            docker_endpoint="/unused/docker.sock",
            project_state_root=source / ".pico",
            sandbox_parent=tmp_path / "sandboxes",
            image=image,
        )

    assert not any((tmp_path / "sandboxes").glob("*/sandbox_*"))


def test_external_source_apply_authority_blocks_replacement_before_docker(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("replacement\n", encoding="utf-8")
    sandbox_parent = tmp_path / "sandboxes"
    workspace = sandbox_parent / ("a" * 24)
    state_root = workspace / ("sandbox_" + "2" * 32)
    state_root.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(
        docker_module.SandboxSessionStore,
        "_workspace_id",
        staticmethod(lambda _source: "a" * 24),
    )
    write_source_apply_authority(
        sandbox_parent,
        source,
        source_device=source.stat().st_dev,
        source_inode=source.stat().st_ino + 1,
        state_root=state_root,
        sandbox_id=state_root.name,
        journal_id="apply_" + "1" * 32,
        diff_digest="sha256:" + "3" * 64,
    )
    image = docker_module.load_image_manifest(
        docker_module.default_image_manifest_path()
    )
    authorization = _development_authorization(monkeypatch, image)
    calls = []
    monkeypatch.setattr(
        docker_module,
        "DockerClient",
        lambda *_args: calls.append("docker") or None,
    )

    with pytest.raises(DockerSandboxError, match="source_apply_review_required"):
        build_docker_sandbox_context(
            source,
            authorization=authorization,
            pico_session_id="session-replacement",
            docker_cli="/unused/docker",
            docker_endpoint="/unused/docker.sock",
            project_state_root=source / ".pico",
            sandbox_parent=sandbox_parent,
            image=image,
        )

    assert calls == []


def test_runtime_splits_roots_and_renders_only_logical_workspace(
    tmp_path,
    monkeypatch,
):
    source, context, agent = _build_runtime(tmp_path, monkeypatch)

    assert agent.source_root == source
    assert agent.execution_root == context.execution_root
    assert agent.root == context.execution_root
    assert agent.project_state_root == source / ".pico"
    assert agent.session["workspace_root"] == str(source)
    assert agent.run_store.root == source / ".pico" / "runs"
    assert agent.checkpoint_store.root == (
        context.sandbox_state_root / "recovery" / ".pico" / "checkpoints"
    )
    assert not (source / ".pico" / "checkpoints").exists()
    assert agent.memory_store.workspace_root == source / ".pico" / "memory"
    assert agent.repo_map.repo_root == context.execution_root
    assert agent.workspace_observer.root == context.execution_root
    assert "git" not in agent.workspace_observer.trusted_executables
    assert agent.path("/workspace/README.md") == context.execution_root / "README.md"
    assert "/workspace" in agent.prefix
    assert str(context.execution_root) not in agent.prefix
    assert str(source) not in agent.prefix
    assert not (context.execution_root / ".env").exists()


def test_model_workspace_never_uses_source_git_metadata(tmp_path, monkeypatch):
    source_only = "SOURCE_ONLY_PRIVATE.key"
    _source, context, agent = _build_runtime(
        tmp_path,
        monkeypatch,
        source_status=f"?? {source_only}",
    )

    assert context.source_status == f"?? {source_only}"
    assert agent.workspace.branch == "pico-sandbox"
    assert agent.workspace.default_branch == "pico-sandbox"
    assert agent.workspace.status == "sandbox_execution_state_unknown"
    assert source_only not in agent.prefix
    assert source_only not in agent.workspace.volatile_text()

    agent.refresh_prefix(force=True)

    assert agent.workspace.status == "sandbox_execution_state_unknown"
    assert source_only not in agent.prefix
    assert source_only not in agent.workspace.volatile_text()


def test_builtin_and_docker_shell_share_staging_without_touching_source(
    tmp_path,
    monkeypatch,
):
    source, context, agent = _build_runtime(tmp_path, monkeypatch)
    source_before = (source / "README.md").read_bytes()

    write = agent.execute_tool(
        "write_file",
        {"path": "/workspace/shared.txt", "content": "from-builtin\n"},
    )
    shell_read = agent.execute_tool(
        "run_shell",
        {"command": "cat shared.txt", "timeout": 5},
    )
    shell_write = agent.execute_tool(
        "run_shell",
        {"command": "printf 'from-shell\\n' > shell.txt", "timeout": 5},
    )
    read = agent.execute_tool(
        "read_file",
        {"path": "/workspace/shell.txt", "start": 1, "end": 10},
    )

    assert write.metadata["tool_status"] == "ok"
    assert "from-builtin" in shell_read.content
    assert shell_write.metadata["sandbox"]["execution_plane"] == "sandbox"
    assert shell_write.metadata["sandbox"]["runner_executed"] is True
    assert "from-shell" in read.content
    assert (source / "README.md").read_bytes() == source_before
    assert not (source / "shared.txt").exists()
    assert not (source / "shell.txt").exists()
    assert (context.execution_root / "shared.txt").read_text() == "from-builtin\n"
    assert (context.execution_root / "shell.txt").read_text() == "from-shell\n"


def test_docker_direct_git_uses_guest_hardening_without_host_git_inspection(
    tmp_path,
    monkeypatch,
):
    _source, context, agent = _build_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        tool_executor_module,
        "_validate_hardened_git_repository",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("host Git inspection reached synthetic repository")
        ),
    )

    result = agent.execute_tool(
        "run_shell",
        {"command": "git add README.md", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "ok"
    target = context.runner.targets[-1]
    assert target[0] == context.runner.image.tool_map["git"]
    assert "core.hooksPath=/dev/null" in target
    assert target[-2:] == ("add", "README.md")


def test_refresh_and_delegate_keep_the_same_execution_root(
    tmp_path,
    monkeypatch,
):
    source, context, agent = _build_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        workspace_module,
        "run_hardened_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("refresh used host Git")
        ),
    )
    captured = {}

    def child_ask(child, task):
        captured.update(
            root=child.root,
            source_root=child.source_root,
            read_only=child.read_only,
            sandbox_context=child.sandbox_context,
            task=task,
        )
        return "done"

    monkeypatch.setattr(Pico, "ask", child_ask)

    project_sessions_before = {
        path.name for path in agent.session_store.root.glob("*.json")
    }
    agent.refresh_prefix(force=True)
    result = agent.spawn_delegate({"task": "inspect README", "max_steps": 1})

    assert agent.workspace.repo_root == str(context.execution_root)
    assert agent.workspace.logical_root == "/workspace"
    assert captured == {
        "root": context.execution_root,
        "source_root": source,
        "read_only": True,
        "sandbox_context": context,
        "task": "inspect README",
    }
    assert result == "delegate_result:\ndone"
    assert {
        path.name for path in agent.session_store.root.glob("*.json")
    } == project_sessions_before
    assert len(
        list(
            (context.sandbox_state_root / "delegate-sessions").glob("*.json")
        )
    ) == 1


def test_resume_reuses_bound_staging_and_current_host_session(
    tmp_path,
    monkeypatch,
):
    source, context, agent = _build_runtime(tmp_path, monkeypatch)
    staged = context.execution_root / "staged-only.txt"
    staged.write_text("candidate\n", encoding="utf-8")
    nonce = context.sandbox_session.manifest["lease"]["owner_nonce"]
    context.runner.session_store.release(context.sandbox_state_root, nonce)

    resumed_context = build_docker_sandbox_context(
        source,
        authorization=context.authorization,
        pico_session_id="session-1",
        docker_cli="/unused/docker",
        docker_endpoint="/unused/docker.sock",
        project_state_root=source / ".pico",
        sandbox_parent=tmp_path / "sandboxes",
        resume=True,
    )
    workspace = WorkspaceContext.build(
        resumed_context.execution_root,
        repo_root_override=resumed_context.execution_root,
        executables=agent.trusted_executables,
        inspect_git=False,
        logical_root=resumed_context.logical_root,
        branch_override="pico-sandbox",
        default_branch_override="pico-sandbox",
        status_override="sandbox_execution_state_unknown",
    )
    resumed = Pico._from_session_for_docker_sandbox_development(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=SessionStore(source / ".pico" / "sessions"),
        session_id="session-1",
        approval_policy="ask",
        redaction_env=MappingProxyType(
            {"OPENAI_API_KEY": "source-secret"}
        ),
        _trusted_redaction_env=True,
        sandbox_context=resumed_context,
        project_config=load_pico_toml(source),
    )

    assert resumed.root == context.execution_root
    assert (resumed.root / "staged-only.txt").read_text() == "candidate\n"
    assert not (source / "staged-only.txt").exists()
    assert len(list((tmp_path / "sandboxes").glob("*/sandbox_*"))) == 1


def test_cleanup_failure_is_a_tool_failure(
    tmp_path,
    monkeypatch,
):
    _source, context, agent = _build_runtime(tmp_path, monkeypatch)
    FakeDockerRunner.cleanup_failure = True

    result = agent.execute_tool(
        "run_shell",
        {"command": "cat README.md", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "error"
    assert result.metadata["tool_error_code"] == "sandbox_cleanup_failed"
    assert result.metadata["sandbox"]["cleanup_status"] == "failed"
    assert result.metadata["sandbox"]["residue_detected"] is True


def test_docker_interrupt_finalizes_tool_change_with_runner_evidence(
    tmp_path,
    monkeypatch,
):
    _source, _context, agent = _build_runtime(tmp_path, monkeypatch)
    primary = KeyboardInterrupt("stop")
    FakeDockerRunner.interrupt = primary

    with pytest.raises(KeyboardInterrupt) as caught:
        agent.execute_tool(
            "run_shell",
            {"command": "cat README.md", "timeout": 5},
        )

    assert caught.value is primary
    record = agent.checkpoint_store.list_tool_change_records(strict=True)[-1]
    assert record["status"] == "interrupted"
    assert record["sandbox"]["status"] == "interrupted"
    assert record["sandbox"]["execution_plane"] == "sandbox"
    assert record["sandbox"]["runner_executed"] is True
    assert record["sandbox"]["target_started"] is True
    assert record["sandbox"]["cleanup_status"] == "completed"
    assert agent._last_tool_result_metadata["tool_status"] == "interrupted"


def test_shell_tool_change_uses_staging_recovery_before_and_after_blobs(
    tmp_path,
    monkeypatch,
):
    source, context, agent = _build_runtime(tmp_path, monkeypatch)

    result = agent.execute_tool(
        "run_shell",
        {"command": "printf 'changed\\n' > README.md", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "ok"
    records = agent.checkpoint_store.list_tool_change_records(strict=True)
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "finalized"
    assert record["recovery_context"]["observer_mode"] == "staging"
    assert record["affected_paths"] == ["README.md"]
    entry = record["file_entries"][0]
    assert agent.checkpoint_store.read_blob(entry["before_blob_ref"]) == b"source\n"
    assert agent.checkpoint_store.read_blob(entry["after_blob_ref"]) == b"changed\n"
    assert agent.checkpoint_store.root.is_relative_to(
        context.sandbox_state_root / "recovery"
    )
    assert not (source / ".pico" / "checkpoints").exists()
    assert (source / "README.md").read_bytes() == b"source\n"


def test_per_call_recovery_restores_staging_without_touching_source(
    tmp_path,
    monkeypatch,
):
    source, context, agent = _build_runtime(tmp_path, monkeypatch)
    result = agent.execute_tool(
        "run_shell",
        {"command": "printf 'changed\\n' > README.md", "timeout": 5},
    )
    record = agent.checkpoint_store.list_tool_change_records(strict=True)[0]
    checkpoint = agent.recovery_checkpoint_writer.create_turn_checkpoint(
        session_id=agent.session["id"],
        run_id="run-1",
        turn_id="turn-1",
        parent_checkpoint_id="",
        tool_change_ids=[record["tool_change_id"]],
    )

    preview = agent.recovery_manager.preview_restore(checkpoint["checkpoint_id"])
    restored = agent.recovery_manager.apply_restore(checkpoint["checkpoint_id"])

    assert result.metadata["tool_status"] == "ok"
    assert preview["status"] == "ready"
    assert restored["status"] == "applied"
    assert (context.execution_root / "README.md").read_bytes() == b"source\n"
    assert (source / "README.md").read_bytes() == b"source\n"


def test_cleanup_failure_still_captures_staging_effect_for_review(
    tmp_path,
    monkeypatch,
):
    _source, _context, agent = _build_runtime(tmp_path, monkeypatch)
    FakeDockerRunner.cleanup_failure = True

    result = agent.execute_tool(
        "run_shell",
        {"command": "printf 'changed\\n' > README.md", "timeout": 5},
    )

    assert result.metadata["tool_status"] == "partial_success"
    record = agent.checkpoint_store.list_tool_change_records(strict=True)[0]
    assert record["status"] == "partial_success"
    assert record["affected_paths"] == ["README.md"]
    assert record["file_entries"][0]["after_hash"]


def test_pending_review_rejects_all_further_model_visible_tools(
    tmp_path,
    monkeypatch,
):
    _source, context, agent = _build_runtime(tmp_path, monkeypatch)
    (context.execution_root / "candidate.txt").write_text(
        "candidate\n",
        encoding="utf-8",
    )
    finalized = agent.workspace_observer.finalize_diff(agent.redact_text)

    result = agent.execute_tool(
        "read_file",
        {"path": "README.md", "start": 1, "end": 5},
    )

    assert finalized["status"] == "diff_ready"
    assert context.current_session().state == "pending_review"
    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "sandbox_session_not_ready"


def test_runtime_finalization_releases_changed_session_for_review(
    tmp_path,
    monkeypatch,
):
    _source, context, agent = _build_runtime(tmp_path, monkeypatch)
    agent.model_client = FakeModelClient(["<final>done</final>"])
    assert agent.ask("prepare review") == "done"
    (context.execution_root / "candidate.txt").write_text(
        "candidate\n",
        encoding="utf-8",
    )

    result = agent.finalize_sandbox_session()
    current = context.current_session()

    assert result["status"] == "diff_ready"
    assert result["session_state"] == "pending_review"
    assert current.state == "pending_review"
    assert current.manifest["lease"] is None
    assert context.execution_root.exists()
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["sandbox"]["session_state"] == "pending_review"
    assert report["sandbox"]["diff"] == {
        "candidates": 1,
        "blocked": 0,
        "generated": 0,
    }


def test_runtime_finalization_auto_discards_no_change_session(
    tmp_path,
    monkeypatch,
):
    _source, context, agent = _build_runtime(tmp_path, monkeypatch)

    result = agent.finalize_sandbox_session()
    current = context.current_session()

    assert result["status"] == "no_changes_discarded"
    assert current.state == "discarded"
    assert current.manifest["lease"] is None
    assert not context.execution_root.exists()


def test_runtime_finalization_failure_enters_review_and_releases_lease(
    tmp_path,
    monkeypatch,
):
    _source, context, agent = _build_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(
        agent.workspace_observer,
        "finalize_diff",
        lambda _redact: (_ for _ in ()).throw(OSError("injected")),
    )

    with pytest.raises(OSError, match="injected"):
        agent.finalize_sandbox_session()

    current = context.current_session()
    assert current.state == "review_required"
    assert current.manifest["lease"] is None
    assert current.manifest["cleanup"]["last_error_code"] == (
        "sandbox_diff_finalization_failed"
    )


def test_docker_runtime_requires_prefrozen_config_and_redaction(
    tmp_path,
    monkeypatch,
):
    source, context, agent = _build_runtime(tmp_path, monkeypatch)
    workspace = agent.workspace
    store = SessionStore(source / ".pico" / "other-sessions")

    with pytest.raises(ValueError, match="runtime context is incomplete"):
        Pico._for_docker_sandbox_development(
            model_client=FakeModelClient([]),
            workspace=workspace,
            session_store=store,
            sandbox_context=context,
            project_config=load_pico_toml(source),
            session_id="session-1",
        )


def test_host_runtime_root_contract_is_unchanged(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    source = tmp_path / "source"
    source.mkdir()
    workspace = WorkspaceContext.build(source)
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=SessionStore(source / ".pico" / "sessions"),
    )

    assert agent.source_root == source
    assert agent.execution_root == source
    assert agent.project_state_root == source / ".pico"
    assert agent.root == source
    assert agent.session["workspace_root"] == str(source)
    assert shutil.which("git") is None or "git" in agent.trusted_executables
