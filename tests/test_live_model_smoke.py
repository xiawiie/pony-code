from types import SimpleNamespace

from scripts.live_model_smoke import (
    _error_result,
    _temporary_project_env,
    classify_live_error,
    main,
    run_live_model_smoke,
    should_fail_all_skipped,
)


def test_classify_live_error_auth():
    assert classify_live_error(RuntimeError("HTTP 401: bad key")) == "auth"


def test_classify_live_error_http_error_401_auth():
    assert classify_live_error(RuntimeError("HTTP Error 401: Unauthorized")) == "auth"


def test_classify_live_error_http_error_403_auth():
    assert classify_live_error(RuntimeError("HTTP Error 403: Forbidden")) == "auth"


def test_classify_live_error_unauthorized_auth():
    assert classify_live_error(RuntimeError("unauthorized: invalid api key")) == "auth"


def test_classify_live_error_authentication_error_auth():
    assert classify_live_error(RuntimeError("authentication_error: invalid x-api-key")) == "auth"


def test_classify_live_error_rate_limit():
    assert classify_live_error(RuntimeError("HTTP 429: rate limit")) == "rate_limit"


def test_classify_live_error_http_error_429_rate_limit():
    assert classify_live_error(RuntimeError("HTTP Error 429: Too Many Requests")) == "rate_limit"


def test_classify_live_error_network():
    assert classify_live_error(RuntimeError("Could not reach Ollama")) == "network"


def test_classify_live_error_defaults_to_code_failure():
    assert classify_live_error(RuntimeError("unexpected decode bug")) == "code_failure"


def test_should_fail_all_skipped_when_everything_skipped():
    assert should_fail_all_skipped([{"status": "skipped"}]) is True


def test_should_fail_all_skipped_when_anything_runs():
    assert should_fail_all_skipped([{"status": "skipped"}, {"status": "ok"}]) is False


def test_error_result_redacts_secret_like_values():
    resolved = SimpleNamespace(name="model", api="openai-chat", base_url="https://api.example.test", api_key="sk-test-secret")
    result = _error_result(
        "/tmp/workspace",
        resolved,
        RuntimeError("Authorization: Bearer abc123 sk-test-secret api_key=sk-test-secret"),
    )

    assert "sk-test-secret" not in result["error"]
    assert "Bearer abc123" not in result["error"]
    assert "<redacted>" in result["error"]


def test_temporary_project_env_restores_and_removes_vars(monkeypatch):
    monkeypatch.setenv("EXISTING_VAR", "before")
    monkeypatch.delenv("NEW_ONLY_VAR", raising=False)

    with _temporary_project_env({"EXISTING_VAR": "during", "NEW_ONLY_VAR": "new"}):
        assert __import__("os").environ["EXISTING_VAR"] == "during"
        assert __import__("os").environ["NEW_ONLY_VAR"] == "new"

    assert __import__("os").environ["EXISTING_VAR"] == "before"
    assert "NEW_ONLY_VAR" not in __import__("os").environ


def test_run_live_model_smoke_exit_mapping(monkeypatch, tmp_path):
    class RaisingClient:
        def __init__(self, message):
            self.message = message

        def complete_v2(self, **kwargs):
            raise RuntimeError(self.message)

    resolved = SimpleNamespace(
        name="model",
        api="openai-chat",
        base_url="https://api.example.test",
        api_key="sk-test-secret",
    )
    monkeypatch.setattr("scripts.live_model_smoke.read_project_env", lambda root, warn=True: {})
    monkeypatch.setattr("scripts.live_model_smoke.load_model_connection", lambda root: object())
    monkeypatch.setattr("scripts.live_model_smoke.resolve_model_connection", lambda connection: resolved)

    cases = [
        ("unauthorized: invalid api key", 0, "auth"),
        ("HTTP Error 429: Too Many Requests", 0, "rate_limit"),
        ("Could not reach Ollama", 0, "network"),
        ("unexpected decode bug", 1, "code_failure"),
    ]
    for message, expected_exit, expected_type in cases:
        monkeypatch.setattr(
            "scripts.live_model_smoke.build_model_client",
            lambda resolved, temperature, top_p, message=message: RaisingClient(message),
        )
        payload, exit_code = run_live_model_smoke(tmp_path)
        assert exit_code == expected_exit
        assert payload["results"][0]["error_type"] == expected_type


def test_main_writes_redacted_artifact_and_stdout(monkeypatch, tmp_path, capsys):
    class RaisingClient:
        def complete_v2(self, **kwargs):
            raise RuntimeError("Authorization: Bearer abc123 unauthorized sk-test-secret token=abc123")

    resolved = SimpleNamespace(
        name="model",
        api="openai-chat",
        base_url="https://api.example.test",
        api_key="sk-test-secret",
    )
    monkeypatch.setattr("scripts.live_model_smoke.read_project_env", lambda root, warn=True: {})
    monkeypatch.setattr("scripts.live_model_smoke.load_model_connection", lambda root: object())
    monkeypatch.setattr("scripts.live_model_smoke.resolve_model_connection", lambda connection: resolved)
    monkeypatch.setattr(
        "scripts.live_model_smoke.build_model_client",
        lambda resolved, temperature, top_p: RaisingClient(),
    )

    exit_code = main([str(tmp_path)])
    captured = capsys.readouterr()
    artifact = (tmp_path / "artifacts" / "live-checks" / "live-model-smoke.json").read_text(encoding="utf-8")

    assert exit_code == 0
    assert "sk-test-secret" not in artifact
    assert "Bearer abc123" not in artifact
    assert "token=abc123" not in artifact
    assert "sk-test-secret" not in captured.out
    assert "Bearer abc123" not in captured.out
    assert "token=abc123" not in captured.out
    assert "<redacted>" in artifact
    assert "<redacted>" in captured.out
