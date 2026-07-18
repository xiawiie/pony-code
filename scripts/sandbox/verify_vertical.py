#!/usr/bin/env python3
"""Run the real local Docker Sandbox security and apply vertical."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import tempfile

from pony.sandbox.apply import SourceApplier, StagingObserver
from pony.sandbox.docker import (
    build_docker_sandbox_context,
    discover_local_docker,
    DockerSandboxError,
    DockerSandboxContext,
    DockerSandboxRuntimeAuthorization,
    DockerImageManifest,
    local_docker_sandbox_runtime,
    MAX_OUTPUT_BYTES,
)
from pony.state.checkpoint_store import CheckpointStore


_FIXTURE_SECRET = "pony-g7-secret-sentinel"
_BOUNDARY_CHECK_COUNT = 7


@dataclass(frozen=True)
class _Fixture:
    session_id: str
    source: Path
    context: DockerSandboxContext
    observer: StagingObserver
    image: DockerImageManifest
    authorization: DockerSandboxRuntimeAuthorization
    docker_cli: str
    docker_endpoint: str


def _observer(context):
    checkpoints = CheckpointStore(
        context.sandbox_state_root / "recovery" / ".pony" / "checkpoints"
    )
    observer = StagingObserver(
        context,
        checkpoints,
        redaction_env={"G7_SECRET": _FIXTURE_SECRET},
        secret_env_names=("G7_SECRET",),
    )
    observer.ensure_baseline()
    return observer


def _create_fixture(root, name, files):
    source = root / name / "source"
    source.mkdir(parents=True)
    for relative, content in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (source / ".env").write_text(
        f"PONY_API_KEY={_FIXTURE_SECRET}\n",
        encoding="utf-8",
    )
    image, authorization = local_docker_sandbox_runtime()
    docker_cli, docker_endpoint = discover_local_docker()
    context = build_docker_sandbox_context(
        source,
        authorization=authorization,
        pony_session_id=name,
        docker_cli=docker_cli,
        docker_endpoint=docker_endpoint,
        project_state_root=source / ".pony",
        sandbox_parent=root / "sandboxes",
        known_secrets=(_FIXTURE_SECRET.encode("utf-8"),),
        source_branch="g7",
        source_status="clean",
        source_default_branch="main",
    )
    return _Fixture(
        session_id=name,
        source=source,
        context=context,
        observer=_observer(context),
        image=image,
        authorization=authorization,
        docker_cli=docker_cli,
        docker_endpoint=docker_endpoint,
    )


def _execute(fixture, script, timeout):
    current = fixture.context.current_session()
    python = dict(fixture.image.tool_paths)["python"]
    plan = fixture.context.runner.compile(
        current,
        [python, "-c", script],
        timeout=timeout,
    )
    return fixture.context.runner.execute(current, plan)


def _require_completed(outcome):
    if (
        outcome.sandbox_outcome != "completed"
        or outcome.exit_code != 0
        or outcome.cleanup_status != "completed"
        or outcome.residue_detected
    ):
        raise RuntimeError("real sandbox command did not complete cleanly")


def _require_no_container(context):
    sandbox_id = context.sandbox_session.sandbox_id
    result = context.runner.client.command(
        [
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label=io.pony.runtime.sandbox={sandbox_id}",
        ]
    )
    if result.exit_code != 0 or result.timed_out or result.stdout.strip():
        raise RuntimeError("real sandbox container cleanup was incomplete")


def _boundary_script(fixture):
    forbidden = [
        str(fixture.source),
        str(fixture.context.sandbox_state_root),
        str(Path.home()),
    ]
    return f"""
import json
import os
from pathlib import Path
import socket

forbidden = {forbidden!r}
mounts = Path('/proc/self/mountinfo').read_text(encoding='utf-8')
environment = Path('/proc/self/environ').read_bytes()
try:
    connection = socket.create_connection(('1.1.1.1', 53), timeout=1)
except OSError:
    network_blocked = True
else:
    network_blocked = False
    connection.close()
Path('/workspace/candidate.txt').write_text('from-real-container\\n')
Path('/workspace/__pycache__').mkdir()
Path('/workspace/__pycache__/generated.pyc').write_bytes(b'generated')
print(json.dumps({{
    'env_filtered': not Path('/workspace/.env').exists(),
    'state_filtered': not Path('/workspace/.pony').exists(),
    'docker_socket_absent': not Path('/var/run/docker.sock').exists(),
    'host_paths_absent': all(value not in mounts for value in forbidden),
    'secret_absent': {_FIXTURE_SECRET!r}.encode() not in environment and not any(
        {_FIXTURE_SECRET!r} in path.read_text(encoding='utf-8', errors='ignore')
        for path in Path('/workspace').rglob('*') if path.is_file()
    ),
    'network_blocked': network_blocked,
    'home_sandboxed': os.environ.get('HOME') == '/home/pony',
}}, sort_keys=True))
""".strip()


def _verify_boundary_and_limits(fixture):
    outcome = _execute(fixture, _boundary_script(fixture), 10)
    _require_completed(outcome)
    checks = json.loads(outcome.stdout.decode("utf-8"))
    if len(checks) != _BOUNDARY_CHECK_COUNT or not all(checks.values()):
        raise RuntimeError("real sandbox boundary check failed")
    if (fixture.source / "candidate.txt").exists():
        raise RuntimeError("real sandbox changed Source Root before apply")
    timeout = _execute(
        fixture,
        "import subprocess,time; subprocess.Popen(['sleep','30']); time.sleep(30)",
        1,
    )
    if (
        timeout.sandbox_outcome != "timeout"
        or not timeout.timed_out
        or timeout.cleanup_status != "completed"
        or timeout.residue_detected
    ):
        raise RuntimeError("real sandbox timeout cleanup failed")
    output = _execute(
        fixture,
        f"import sys; sys.stdout.write('x' * {MAX_OUTPUT_BYTES + 1})",
        10,
    )
    _require_completed(output)
    if not output.stdout_truncated or output.stdout_bytes <= MAX_OUTPUT_BYTES:
        raise RuntimeError("real sandbox output bound was not enforced")
    _require_no_container(fixture.context)


def _redact_fixture(text):
    return text.replace(_FIXTURE_SECRET, "[REDACTED]")


def _verify_diff_apply_and_terminal_resume(root):
    fixture = _create_fixture(root, "g7-apply", {"README.md": "source\n"})
    _verify_boundary_and_limits(fixture)
    finalized = fixture.observer.finalize_diff(_redact_fixture)
    counts = finalized["artifact"]["counts"]
    if counts["candidate"] != 1 or finalized["generated_count"] < 1:
        raise RuntimeError("real sandbox final capture was incomplete")
    applied = SourceApplier(fixture.context, fixture.observer).apply(
        finalized["diff_digest"]
    )
    if applied["status"] != "apply_applied":
        raise RuntimeError("real sandbox Source Apply failed")
    if (fixture.source / "candidate.txt").read_text() != "from-real-container\n":
        raise RuntimeError("real sandbox Source Apply content mismatch")
    resumed = _resume_terminal_fixture(root, fixture)
    if (
        resumed.resumed
        or resumed.sandbox_session.sandbox_id
        == fixture.context.sandbox_session.sandbox_id
        or (resumed.execution_root / "candidate.txt").read_text()
        != "from-real-container\n"
    ):
        raise RuntimeError("terminal sandbox resume did not create fresh staging")
    resumed.runner.session_store.discard(resumed.sandbox_state_root)
    _require_no_container(fixture.context)
    _require_no_container(resumed)


def _resume_terminal_fixture(root, fixture):
    return build_docker_sandbox_context(
        fixture.source,
        authorization=fixture.authorization,
        pony_session_id=fixture.session_id,
        docker_cli=fixture.docker_cli,
        docker_endpoint=fixture.docker_endpoint,
        project_state_root=fixture.source / ".pony",
        sandbox_parent=root / "sandboxes",
        known_secrets=(_FIXTURE_SECRET.encode("utf-8"),),
        resume=True,
        source_branch="g7",
        source_status="clean",
        source_default_branch="main",
    )


def _verify_apply_conflict_and_resume_block(root):
    fixture = _create_fixture(root, "g7-conflict", {"target.txt": "before\n"})
    outcome = _execute(
        fixture,
        "from pathlib import Path; "
        "Path('/workspace/target.txt').write_text('candidate\\n')",
        10,
    )
    _require_completed(outcome)
    finalized = fixture.observer.finalize_diff(_redact_fixture)
    (fixture.source / "target.txt").write_text("external\n", encoding="utf-8")
    conflict = SourceApplier(fixture.context, fixture.observer).apply(
        finalized["diff_digest"]
    )
    if (
        conflict["status"] != "apply_conflicted"
        or (fixture.source / "target.txt").read_text() != "external\n"
    ):
        raise RuntimeError("real sandbox CAS conflict did not fail closed")
    try:
        _resume_terminal_fixture(root, fixture)
    except DockerSandboxError as exc:
        if exc.code != "sandbox_resume_invalid":
            raise
    else:
        raise RuntimeError("unreviewed sandbox diff was resumable")
    fixture.context.runner.session_store.discard(
        fixture.context.sandbox_state_root
    )
    _require_no_container(fixture.context)


def _verify_sensitive_diff_block(root):
    fixture = _create_fixture(root, "g7-sensitive", {"README.md": "source\n"})
    script = (
        "from pathlib import Path; "
        "Path('/workspace/.env').write_text('TOKEN=blocked\\n'); "
        f"Path('/workspace/known.txt').write_text({_FIXTURE_SECRET!r})"
    )
    outcome = _execute(fixture, script, 10)
    _require_completed(outcome)
    finalized = fixture.observer.finalize_diff(_redact_fixture)
    counts = finalized["artifact"]["counts"]
    blocked = SourceApplier(fixture.context, fixture.observer).apply(
        finalized["diff_digest"]
    )
    raw_diff = (
        fixture.context.sandbox_state_root / "recovery" / "diff.json"
    ).read_bytes()
    if (
        finalized["status"] != "diff_blocked"
        or counts["blocked_sensitive"] != 2
        or blocked["status"] != "diff_blocked"
        or _FIXTURE_SECRET.encode("utf-8") in raw_diff
        or (fixture.source / "known.txt").exists()
    ):
        raise RuntimeError("real sandbox sensitive diff did not fail closed")
    fixture.context.runner.session_store.discard(
        fixture.context.sandbox_state_root
    )
    _require_no_container(fixture.context)


def verify_vertical():
    with tempfile.TemporaryDirectory(
        prefix="pony-g7-vertical-",
        dir="/private/tmp",
    ) as raw_root:
        root = Path(raw_root)
        _verify_diff_apply_and_terminal_resume(root)
        _verify_apply_conflict_and_resume_block(root)
        _verify_sensitive_diff_block(root)
    return {
        "record_type": "pony_sandbox_vertical_verification",
        "format_version": 1,
        "status": "passed",
        "boundary_checks": _BOUNDARY_CHECK_COUNT,
        "container_calls": 9,
        "target_container_calls": 5,
        "network": "blocked",
        "cleanup": "complete",
        "source_apply": "passed",
        "cas_conflict": "blocked",
        "sensitive_diff": "blocked",
        "terminal_resume": "fresh_staging",
        "unreviewed_resume": "blocked",
    }


def main():
    try:
        payload = verify_vertical()
    except (OSError, RuntimeError, DockerSandboxError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        print(json.dumps({"status": "failed", "error_code": code}, sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
