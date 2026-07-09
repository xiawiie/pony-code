import pytest

from pico.model_config import ModelConnection, ModelConnectionConfigError, load_model_connection


def test_load_model_connection_from_pico_toml(tmp_path, monkeypatch):
    (tmp_path / "pico.toml").write_text(
        """
[model]
name = "qwen-max"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")

    config = load_model_connection(tmp_path)

    assert config.name == "qwen-max"
    assert config.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert config.api_key_env == "DASHSCOPE_API_KEY"
    assert config.api_key == "sk-test"
    assert config.api is None


def test_load_model_connection_trims_api_key_env_value(tmp_path, monkeypatch):
    (tmp_path / "pico.toml").write_text(
        """
[model]
name = "qwen-max"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "  sk-spaced  ")

    config = load_model_connection(tmp_path)

    assert config.api_key == "sk-spaced"


def test_load_model_connection_optional_api_and_timeout(tmp_path, monkeypatch):
    (tmp_path / "pico.toml").write_text(
        """
[model]
name = "custom-model"
base_url = "https://llm.example.test/v1"
api_key_env = "CUSTOM_API_KEY"
api = "openai-chat"
timeout = 45
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CUSTOM_API_KEY", "sk-test")

    config = load_model_connection(tmp_path)

    assert config.api == "openai-chat"
    assert config.timeout == 45


def test_raw_api_key_in_pico_toml_is_rejected(tmp_path):
    (tmp_path / "pico.toml").write_text(
        """
[model]
name = "qwen-max"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key = "sk-raw-secret"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ModelConnectionConfigError, match="api_key_env"):
        load_model_connection(tmp_path)


def test_invalid_api_key_env_is_rejected_without_echoing_value(tmp_path):
    (tmp_path / "pico.toml").write_text(
        """
[model]
name = "qwen-max"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "sk-raw-looking-secret"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ModelConnectionConfigError) as exc_info:
        load_model_connection(tmp_path)

    message = str(exc_info.value)
    assert "api_key_env" in message
    assert "sk-raw-looking-secret" not in message


def test_model_connection_repr_does_not_expose_api_key():
    config = ModelConnection(
        name="qwen-max",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        api_key="sk-secret",
    )

    assert "sk-secret" not in repr(config)


def test_missing_model_section_uses_local_ollama_default(tmp_path):
    config = load_model_connection(tmp_path)

    assert config.name == "qwen3.5:4b"
    assert config.base_url == "http://127.0.0.1:11434"
    assert config.api_key_env == ""
    assert config.api_key == ""


def test_missing_named_api_key_env_fails_before_request(tmp_path, monkeypatch):
    (tmp_path / "pico.toml").write_text(
        """
[model]
name = "glm-4.6"
base_url = "https://open.bigmodel.cn/api/paas/v4"
api_key_env = "ZAI_API_KEY"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ZAI_API_KEY", raising=False)

    with pytest.raises(ModelConnectionConfigError, match="ZAI_API_KEY"):
        load_model_connection(tmp_path)


def test_whitespace_only_api_key_env_fails_before_request(tmp_path, monkeypatch):
    (tmp_path / "pico.toml").write_text(
        """
[model]
name = "glm-4.6"
base_url = "https://open.bigmodel.cn/api/paas/v4"
api_key_env = "ZAI_API_KEY"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZAI_API_KEY", "   ")

    with pytest.raises(ModelConnectionConfigError, match="ZAI_API_KEY"):
        load_model_connection(tmp_path)
