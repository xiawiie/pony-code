from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import stat
import sys
from unittest.mock import Mock

import pytest

from pico import Pico, SessionStore, WorkspaceContext
from pico.cli import main
from pico.cli_start import run_agent_once
from pico.config import write_project_env_assignments
from pico.providers.response import Response, StopReason
from pico.tools import _ApprovedShellExecution


class ScriptedProvider:
    supports_prompt_cache = False
    last_completion_metadata = {}

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, **request):
        self.requests.append(deepcopy(request))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(stop_reason, *content):
    return Response(stop_reason=stop_reason, content=list(content), usage={})


def _build_agent(root, provider, *, session=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=provider,
        workspace=WorkspaceContext.build(
            root,
            executables={
                "python": sys.executable,
                "pytest": sys.executable,
                "sh": sys.executable,
            },
        ),
        session_store=SessionStore(root / ".pico" / "sessions"),
        approval_policy="ask",
        max_steps=6,
        session=session,
    )


def _assert_private_tree(root):
    if os.name != "posix":
        return
    assert stat.S_IMODE(root.lstat().st_mode) == 0o700, root
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            assert stat.S_IMODE(mode) == 0o700, path
        elif stat.S_ISREG(mode):
            assert stat.S_IMODE(mode) == 0o600, path


def test_readme_states_post_validation_and_platform_trust_boundaries():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )

    assert "Git marker、结构元数据、config 或 index" in readme
    assert "校验后并发修改" in readme
    assert "不是 OS sandbox 或 immutable snapshot" in readme
    assert "POSIX/macOS" in readme
    assert "所需安全原语不可用时 fail closed" in readme
    assert "Windows 等价机制留待后续设计" in readme


def test_offline_a1_canary_crosses_real_boundaries_without_normal_artifact_leak(
    tmp_path,
    monkeypatch,
    capsys,
    caplog,
):
    secret = "ghp_" + "A" * 32
    safe_blob_bytes = b"safe recovery bytes\n"
    monkeypatch.setenv("PICO_DEEPSEEK_API_KEY", secret)
    monkeypatch.setattr(
        "pico.cli_diagnostics.check_provider_connectivity",
        Mock(side_effect=AssertionError("offline doctor attempted network")),
    )
    write_project_env_assignments(tmp_path, {"PICO_PROVIDER": "deepseek"})

    candidate = {
        "record_type": "session",
        "format_version": 1,
        "id": "candidate-canary",
        "created_at": "2026-01-01T00:00:00+00:00",
        "workspace_root": str(tmp_path),
        "messages": [
            {"role": "user", "content": secret, "_pico_meta": {}}
        ],
        "working_memory": {
            "task_summary": secret,
            "recent_files": [secret],
        },
        "memory": {},
        "recently_recalled": [],
        "checkpoints": {},
        "resume_state": {},
        "recovery": {},
        "runtime_identity": {},
    }
    provider = ScriptedProvider(
        [
            _response(
                StopReason.STOP_SEQUENCE,
                {"type": "text", "text": secret},
            ),
            _response(
                StopReason.TOOL_USE,
                {
                    "type": "tool_use",
                    "id": "toolu_raw",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                },
            ),
            _response(
                StopReason.TOOL_USE,
                {
                    "type": "tool_use",
                    "id": "toolu_blocked",
                    "name": "delegate",
                    "input": {"task": secret},
                },
            ),
            _response(
                StopReason.TOOL_USE,
                {
                    "type": "tool_use",
                    "id": "toolu_verify",
                    "name": "run_shell",
                    "input": {
                        "command": "python -m pytest -q",
                        "timeout": 5,
                    },
                },
            ),
            _response(
                StopReason.END_TURN,
                {"type": "text", "text": "final " + secret},
            ),
        ]
    )
    agent = _build_agent(tmp_path, provider, session=candidate)
    assert secret not in json.dumps(agent.session)
    assert secret not in json.dumps(agent.memory.to_dict())

    raw_result_runner = Mock(return_value="raw tool result " + secret)
    blocked_delegate_runner = Mock(return_value="must not delegate")
    verification_runner = Mock(
        return_value={
            "stdout": "verification stdout " + secret,
            "stderr": "verification stderr " + secret,
            "exit_code": 0,
        }
    )
    approval_payloads = []

    def approve(name, payload):
        approval_payloads.append((name, deepcopy(payload)))
        return True

    agent.approve = approve
    agent.tools["read_file"]["run"] = raw_result_runner
    agent.tools["delegate"]["run"] = blocked_delegate_runner
    agent.tools["run_shell"]["run"] = verification_runner

    checkpoint_writes = []
    write_checkpoint = agent.checkpoint_store.write_checkpoint_record

    def capture_checkpoint_write(record):
        checkpoint_writes.append(deepcopy(record))
        assert secret not in json.dumps(record)
        return write_checkpoint(record)

    monkeypatch.setattr(
        agent.checkpoint_store,
        "write_checkpoint_record",
        capture_checkpoint_write,
    )

    assert run_agent_once(agent, ["user request", secret]) == 0
    visible = capsys.readouterr().out
    assert secret not in visible
    assert len(provider.requests) == 5
    assert secret not in json.dumps(provider.requests)
    assert raw_result_runner.call_count == 1
    blocked_delegate_runner.assert_not_called()
    assert verification_runner.call_count == 1
    assert [name for name, _ in approval_payloads] == ["run_shell"]
    assert secret not in json.dumps(approval_payloads)
    assert any(
        record.get("verification_evidence")
        for record in checkpoint_writes
    )
    assert secret not in json.dumps(checkpoint_writes)

    checkpoint_id = agent.current_task_state.recovery_checkpoint_id
    run_id = agent.current_task_state.run_id
    session_id = agent.session["id"]
    checkpoint = agent.checkpoint_store.load_checkpoint_record(checkpoint_id)
    assert len(checkpoint["verification_evidence"]) == 1
    evidence = checkpoint["verification_evidence"][0]
    assert evidence["command"] == "python -m pytest -q"
    assert evidence["status"] == "passed"
    assert secret not in json.dumps(evidence)

    # One approved shell call carries the canary through the real approval and
    # ToolExecutor error path. The callback and persisted Tool Change stay safe.
    approval_payloads.clear()
    failing_runner = Mock(side_effect=RuntimeError("runner failure " + secret))
    agent.tools["run_shell"]["run"] = failing_runner
    failed = agent.execute_tool(
        "run_shell",
        {
            "command": f"python -m pytest --approval-note={secret}",
            "timeout": 5,
        },
    )
    assert failing_runner.call_count == 1
    assert isinstance(failing_runner.call_args.args[0], _ApprovedShellExecution)
    assert secret not in json.dumps(approval_payloads)
    assert failed.metadata["tool_change_id"]
    failed_change = agent.checkpoint_store.load_tool_change_record(
        failed.metadata["tool_change_id"]
    )
    assert secret not in json.dumps(failed_change)
    assert secret not in failed.content
    assert secret not in json.dumps(agent._last_tool_result_metadata)

    # Real snapshot flows: sensitive after-bytes hit snapshot eligibility but
    # never reach write_blob, while a separate safe file is byte-exact.
    blob_inputs = []
    write_blob = agent.checkpoint_store.write_blob

    def capture_blob(data, content_kind="text"):
        blob_inputs.append(bytes(data))
        assert secret.encode() not in data
        return write_blob(data, content_kind)

    monkeypatch.setattr(agent.checkpoint_store, "write_blob", capture_blob)
    sensitive_file = tmp_path / "source.txt"
    sensitive_file.write_text("safe before\n", encoding="utf-8")

    def create_sensitive(_execution):
        sensitive_file.write_text(secret, encoding="utf-8")
        return {"stdout": "changed", "stderr": "", "exit_code": 0}

    agent.tools["run_shell"]["run"] = Mock(side_effect=create_sensitive)
    sensitive_result = agent.execute_tool(
        "run_shell",
        {"command": "python create-sensitive", "timeout": 5},
    )
    sensitive_entry = next(
        entry
        for entry in sensitive_result.metadata["file_entries"]
        if entry["path"] == "source.txt"
    )
    assert sensitive_entry["ineligible_reason"] == "mode_unknown"
    assert not sensitive_entry["after_blob_ref"]
    sensitive_file.write_text("clean\n", encoding="utf-8")

    safe_file = tmp_path / "safe-created.txt"

    def create_safe(_execution):
        safe_file.write_bytes(safe_blob_bytes)
        return {"stdout": "created", "stderr": "", "exit_code": 0}

    agent.tools["run_shell"]["run"] = Mock(side_effect=create_safe)
    safe_result = agent.execute_tool(
        "run_shell",
        {"command": "python create-safe", "timeout": 5},
    )
    safe_entry = next(
        entry
        for entry in safe_result.metadata["file_entries"]
        if entry["path"] == "safe-created.txt"
    )
    assert safe_entry["after_blob_ref"]
    assert agent.checkpoint_store.read_blob(safe_entry["after_blob_ref"]) == (
        safe_blob_bytes
    )
    assert safe_blob_bytes in blob_inputs
    assert all(
        secret.encode() not in path.read_bytes()
        for path in agent.checkpoint_store.blobs_dir.rglob("*")
        if path.is_file()
    )

    # A provider error crosses the real one-shot CLI boundary and is absent
    # from visible output and persisted run artifacts.
    error_root = tmp_path / "provider-error"
    error_provider = ScriptedProvider(
        [RuntimeError("provider failure " + secret)]
    )
    error_agent = _build_agent(error_root, error_provider)
    caplog.set_level(logging.DEBUG, logger="pico")
    assert run_agent_once(error_agent, ["provider error", secret]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err + caplog.text
    assert error_provider.requests
    assert secret not in json.dumps(error_provider.requests)

    # Runtime load is strict: legacy bytes are neither rewritten nor backed up.
    legacy_id = "legacy-canary"
    legacy = {
        "id": legacy_id,
        "schema_version": 1,
        "history": [{"role": "user", "content": secret}],
    }
    legacy_raw = json.dumps(legacy).encode("utf-8")
    legacy_path = agent.session_store.path(legacy_id)
    legacy_path.write_bytes(legacy_raw)
    with pytest.raises(ValueError, match="session payload|format version|required"):
        agent.session_store.load(legacy_id)
    assert legacy_path.read_bytes() == legacy_raw
    assert not (agent.session_store.root / "backup").exists()

    commands = (
        ("sessions", "show", session_id),
        ("runs", "show", run_id),
        ("checkpoints", "show", checkpoint_id),
        ("doctor", "--offline"),
    )
    for output_format in ("json", "text"):
        for command in commands:
            assert main(
                [
                    "--cwd",
                    str(tmp_path),
                    "--format",
                    output_format,
                    *command,
                ]
            ) == 0
            cli_output = capsys.readouterr()
            assert secret not in cli_output.out + cli_output.err

    assert session_id
    assert run_id
    assert checkpoint_id
    assert failed.metadata["tool_change_id"]
    assert secret not in json.dumps(agent.session)
    assert secret not in json.dumps(agent.memory.to_dict())

    holders = []
    for pico_root in (tmp_path / ".pico", error_root / ".pico"):
        for path in pico_root.rglob("*"):
            if path.is_file() and secret.encode() in path.read_bytes():
                holders.append(path)
    assert holders == [legacy_path]

    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    _assert_private_tree(tmp_path / ".pico")
    _assert_private_tree(error_root / ".pico")
