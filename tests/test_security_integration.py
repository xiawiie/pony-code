import json
import os
from unittest.mock import Mock

import pytest

from pony.providers.response import Response, StopReason
from pony.runtime.application import Pony
from pony.state.session_store import SessionStore
from pony.state.task_state import TaskState
from pony.workspace.context import WorkspaceContext
from pony.runtime.options import RuntimeOptions


def _sentinel():
    return "ghp_" + "A" * 32


class CapturingClient:
    supports_prompt_cache = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, **request):
        self.requests.append(request)
        return self.responses.pop(0)


def _agent(tmp_path, client, *, permission_mode="auto"):
    (tmp_path / "README.md").write_text("safe fixture\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    agent = Pony(
        model_client=client,
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".pony" / "sessions"),
        options=RuntimeOptions(
            project_trusted=True,
            secret_env_names=("PONY_TEST_TOKEN",),
        ),
    )
    if permission_mode != "auto":
        agent.set_permission_mode(permission_mode)
    return agent


def _normal_artifact_files(root):
    for path in sorted((root / ".pony").rglob("*")):
        if not path.is_file():
            continue
        if "/sessions/backup/" in path.as_posix():
            continue
        yield path


def test_canary_is_absent_from_provider_session_and_normal_artifacts(
    tmp_path, monkeypatch
):
    secret = _sentinel()
    monkeypatch.setenv("PONY_TEST_TOKEN", secret)
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


def test_cli_approval_and_verification_observations_hide_canary(
    tmp_path, monkeypatch, capsys
):
    secret = _sentinel()
    monkeypatch.setenv("PONY_TEST_TOKEN", secret)
    client = CapturingClient(
        [
            Response(
                stop_reason=StopReason.END_TURN,
                content=[{"type": "text", "text": "done"}],
            )
        ]
    )
    agent = _agent(tmp_path, client, permission_mode="default")
    state = TaskState.create(
        run_id="run_canary",
        task_id="task_canary",
        user_request=secret,
    )
    agent.run_store.start_run(state)
    agent.emit_trace(state, "canary", {"token": secret})
    agent.run_store.write_report(state, {"token": secret})
    prompts = []
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or "n",
    )
    assert (
        agent.approve(
        "run_shell",
        {"command": "printf safe", "token": secret},
        )
        is False
    )
    assert secret not in "".join(prompts)

    assert secret not in capsys.readouterr().out


def test_secret_bearing_tool_action_is_blocked_before_runner(tmp_path, monkeypatch):
    secret = _sentinel()
    monkeypatch.setenv("PONY_TEST_TOKEN", secret)
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


def test_runtime_session_load_refuses_legacy_canary_without_backup_or_rewrite(
    tmp_path, monkeypatch
):
    if os.name != "posix":
        pytest.skip("POSIX permission assertion")
    secret = _sentinel()
    monkeypatch.setenv("PONY_TEST_TOKEN", secret)
    store = SessionStore(tmp_path / ".pony" / "sessions")
    store.lock_path.touch(mode=0o600)
    session_id = "legacy-canary"
    legacy_path = store.root / (session_id + ".json")
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        json.dumps(
            {
                "id": session_id,
                "schema_version": 1,
                "history": [{"role": "user", "content": secret}],
            }
        ),
        encoding="utf-8",
    )
    from pony.security.redaction import redact_artifact

    store.set_redactor(
        lambda value: redact_artifact(
            value,
            secret_env_names=("PONY_TEST_TOKEN",),
        )
    )

    original = legacy_path.read_bytes()
    with pytest.raises(ValueError, match="session payload|format version|required"):
        store.load(session_id)
    assert not (store.root / "backup").exists()
    assert legacy_path.read_bytes() == original


def test_provider_error_log_cli_and_run_artifacts_hide_canary(
    tmp_path, monkeypatch, caplog, capsys
):
    secret = _sentinel()
    monkeypatch.setenv("PONY_TEST_TOKEN", secret)

    class FailingClient:
        supports_prompt_cache = False

        def complete(self, **request):
            raise RuntimeError(
                "HTTP 500 body="
                + secret
                + " url=https://user:"
                + secret
                + "@example.invalid/v1?api_key="
                + secret
            )

    agent = _agent(tmp_path, FailingClient())

    with pytest.raises(RuntimeError):
        agent.ask("trigger provider error")

    captured = capsys.readouterr()
    observed = caplog.text + captured.out + captured.err
    assert secret not in observed
    for path in _normal_artifact_files(tmp_path):
        assert secret.encode() not in path.read_bytes(), path
