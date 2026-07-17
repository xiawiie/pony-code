import os

import pytest

from pico.config.model import (
    API_BASE_ENV_NAME,
    API_KEY_ENV_NAME,
    DEFAULT_API_BASE,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    MODEL_ENV_NAME,
    SUPPORTED_PROVIDERS,
    resolve_model_config,
    validate_api_base,
)
from pico.config.project import load_pico_toml
from pico.recovery.policy import DEFAULT_MAX_BLOB_SIZE, snapshot_eligibility


def test_load_pico_toml_reads_simple_project_overrides(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[policy]\nmax_blob_size = 2048\n",
        encoding="utf-8",
    )

    assert load_pico_toml(tmp_path)["policy"]["max_blob_size"] == 2048


def test_project_max_blob_size_falls_back_to_default_when_missing(tmp_path):
    assert load_pico_toml(tmp_path)["policy"]["max_blob_size"] == DEFAULT_MAX_BLOB_SIZE


def test_pico_toml_max_blob_size_overrides_snapshot_eligibility(tmp_path):
    file_rel = "notes/large.md"
    file_abs = tmp_path / file_rel
    file_abs.parent.mkdir(parents=True, exist_ok=True)
    file_abs.write_text("x" * 300, encoding="utf-8")

    assert snapshot_eligibility(tmp_path, file_rel)["snapshot_eligible"] is True
    (tmp_path / "pico.toml").write_text(
        "[policy]\nmax_blob_size = 100\n",
        encoding="utf-8",
    )

    limit = load_pico_toml(tmp_path)["policy"]["max_blob_size"]
    tightened = snapshot_eligibility(tmp_path, file_rel, max_blob_size=limit)
    assert limit == 100
    assert tightened["snapshot_eligible"] is False
    assert tightened["ineligible_reason"] == "file_too_large"


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo", "directory"))
def test_pico_toml_unsafe_entry_warns_and_uses_defaults(
    tmp_path,
    capsys,
    kind,
):
    outside = tmp_path / "outside.toml"
    outside.write_text("[policy]\nmax_blob_size = 1\n", encoding="utf-8")
    target = tmp_path / "pico.toml"
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        sibling = tmp_path.parent / f"{tmp_path.name}-outside.toml"
        sibling.write_text("[policy]\nmax_blob_size = 1\n", encoding="utf-8")
        os.link(sibling, target)
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    else:
        target.mkdir()

    config = load_pico_toml(tmp_path)

    assert config["policy"]["max_blob_size"] == DEFAULT_MAX_BLOB_SIZE
    assert capsys.readouterr().err == "warning: invalid pico.toml; using defaults\n"


def test_pico_toml_over_one_mib_warns_and_uses_defaults(tmp_path, capsys):
    (tmp_path / "pico.toml").write_bytes(b"#" * (1024 * 1024 + 1))

    config = load_pico_toml(tmp_path)

    assert config["policy"]["max_blob_size"] == DEFAULT_MAX_BLOB_SIZE
    assert capsys.readouterr().err == "warning: invalid pico.toml; using defaults\n"


def test_diagnostics_use_static_anthropic_defaults():
    resolved = resolve_model_config(
        project_env={},
        process_env={},
        required=False,
    )

    assert resolved["provider"]["value"] == DEFAULT_PROVIDER
    assert resolved["model"]["value"] == DEFAULT_MODEL
    assert resolved["protocol"]["value"] == "anthropic_messages"
    assert resolved["api_variant"]["value"] == "messages"
    assert resolved["auth_mode"]["value"] == "x-api-key"
    assert resolved["base_url"]["value"] == DEFAULT_API_BASE


def test_diagnostics_use_official_base_when_only_key_is_configured():
    resolved = resolve_model_config(
        project_env={API_KEY_ENV_NAME: "project-key"},
        process_env={},
        required=False,
    )

    assert resolved["base_url"] == {
        "value": DEFAULT_API_BASE,
        "source": "default",
        "name": "anthropic_default_api_base",
    }


@pytest.mark.parametrize(
    ("api_base", "provider", "protocol", "variant", "auth_mode", "model"),
    [
        (
            "https://api.anthropic.com/v1",
            "anthropic",
            "anthropic_messages",
            "messages",
            "x-api-key",
            "claude-sonnet-4-6",
        ),
        (
            "https://api.openai.com/v1",
            "openai",
            "openai_responses",
            "responses",
            "bearer",
            "gpt-5.4",
        ),
        (
            "http://127.0.0.1:11434",
            "ollama",
            "ollama_chat",
            "chat",
            "none",
            "qwen3:8b",
        ),
        (
            "https://gateway.example/v1",
            "openai",
            "openai_chat_completions",
            "chat_completions",
            "bearer",
            "gpt-5.4",
        ),
        (
            "http://127.0.0.1:8080/v1",
            "openai",
            "openai_chat_completions",
            "chat_completions",
            "bearer",
            "gpt-5.4",
        ),
    ],
)
def test_api_base_resolves_the_transport(
    api_base, provider, protocol, variant, auth_mode, model
):
    env = {API_BASE_ENV_NAME: api_base}
    if auth_mode != "none":
        env[API_KEY_ENV_NAME] = "test-key"

    resolved = resolve_model_config(
        project_env=env,
        process_env={},
        required=False,
    )

    assert resolved["provider"]["value"] == provider
    assert resolved["protocol"]["value"] == protocol
    assert resolved["api_variant"]["value"] == variant
    assert resolved["auth_mode"]["value"] == auth_mode
    assert resolved["model"]["value"] == model
    assert resolved["base_url"]["value"] == api_base


def test_generic_https_base_selects_openai_chat_completions():
    resolved = resolve_model_config(
        project_env={
            API_BASE_ENV_NAME: "https://gateway.example/v1",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
        required=False,
    )

    assert resolved["protocol"]["value"] == "openai_chat_completions"
    assert resolved["api_variant"]["value"] == "chat_completions"
    assert "reasoning_replay" not in resolved["capabilities"]


def test_project_env_wins_over_process_env_for_all_three_fields():
    resolved = resolve_model_config(
        project_env={
            MODEL_ENV_NAME: "project-model",
            API_BASE_ENV_NAME: "https://project.example/v1",
            API_KEY_ENV_NAME: "project-key",
        },
        process_env={
            MODEL_ENV_NAME: "process-model",
            API_BASE_ENV_NAME: "https://api.anthropic.com/v1",
            API_KEY_ENV_NAME: "process-key",
        },
    )

    assert resolved["provider"]["value"] == "openai"
    assert resolved["model"]["value"] == "project-model"
    assert resolved["protocol"]["value"] == "openai_chat_completions"
    assert resolved["auth_mode"]["value"] == "bearer"
    assert resolved["base_url"] == {
        "value": "https://project.example/v1",
        "source": "project_env",
        "name": API_BASE_ENV_NAME,
    }
    assert resolved["api_key"] == {
        "value": "project-key",
        "source": "project_env",
        "name": API_KEY_ENV_NAME,
    }


def test_process_env_is_used_when_project_values_are_absent():
    resolved = resolve_model_config(
        project_env={},
        process_env={
            MODEL_ENV_NAME: "process-model",
            API_BASE_ENV_NAME: "https://process.example/v1/",
            API_KEY_ENV_NAME: "process-key",
        },
    )

    assert resolved["base_url"]["value"] == "https://process.example/v1"
    assert resolved["api_key"]["value"] == "process-key"


def test_blank_project_value_blocks_same_named_process_value():
    resolved = resolve_model_config(
        project_env={API_KEY_ENV_NAME: ""},
        process_env={API_KEY_ENV_NAME: "process-key"},
        required=False,
    )

    assert resolved["api_key"] == {
        "value": "",
        "source": "project_env",
        "name": API_KEY_ENV_NAME,
    }


def test_only_legacy_or_vendor_variables_cannot_configure_runtime():
    legacy = {
        "PICO_PROFILE": "deepseek",
        "PICO_CONNECTION": "old",
        "PICO_DEEPSEEK_API_BASE": "https://legacy.example/v1",
        "PICO_DEEPSEEK_MODEL": "legacy-model",
        "PICO_DEEPSEEK_API_KEY": "deepseek-key",
        "OPENAI_API_KEY": "openai-key",
        "ANTHROPIC_API_KEY": "anthropic-key",
        "PICO_PROVIDER": "anthropic",
        "PICO_API_URL": "https://api.anthropic.com/v1",
        "PICO_API_VARIANT": "messages",
        "PICO_AUTH_MODE": "x-api-key",
    }

    with pytest.raises(ValueError, match="^api_base_not_configured$"):
        resolve_model_config(project_env=legacy, process_env={})

    inspected = resolve_model_config(
        project_env=legacy,
        process_env={},
        required=False,
    )
    assert inspected["base_url"] == {
        "value": DEFAULT_API_BASE,
        "source": "default",
        "name": "anthropic_default_api_base",
    }
    assert inspected["api_key"] == {"value": "", "source": "unset", "name": ""}


def test_missing_key_is_allowed_only_for_read_only_diagnostics():
    with pytest.raises(ValueError, match="^api_base_not_configured$"):
        resolve_model_config(project_env={}, process_env={})

    assert (
        resolve_model_config(project_env={}, process_env={}, required=False)["api_key"][
            "value"
        ]
        == ""
    )


@pytest.mark.parametrize(
    ("missing_name", "reason"),
    [
        (API_BASE_ENV_NAME, "api_base_not_configured"),
        (MODEL_ENV_NAME, "model_not_configured"),
        (API_KEY_ENV_NAME, "api_key_not_configured"),
    ],
)
def test_runtime_requires_each_provider_connection_setting(missing_name, reason):
    env = {
        API_BASE_ENV_NAME: "https://api.anthropic.com/v1",
        MODEL_ENV_NAME: "claude-test",
        API_KEY_ENV_NAME: "test-key",
    }
    env.pop(missing_name)

    with pytest.raises(ValueError, match=f"^{reason}$"):
        resolve_model_config(project_env=env, process_env={})


def test_ollama_does_not_require_an_api_key():
    resolved = resolve_model_config(
        project_env={
            API_BASE_ENV_NAME: "http://127.0.0.1:11434",
            MODEL_ENV_NAME: "qwen3:8b",
        },
        process_env={},
    )

    assert resolved["auth_mode"]["value"] == "none"
    assert resolved["api_key"]["value"] == ""


@pytest.mark.parametrize(
    "api_base", ["https://api.anthropic.com/v1", "https://api.openai.com/v1"]
)
def test_cloud_providers_require_key(api_base):
    with pytest.raises(ValueError, match="^api_key_not_configured$"):
        resolve_model_config(
            project_env={
                API_BASE_ENV_NAME: api_base,
                MODEL_ENV_NAME: "cloud-test-model",
            },
            process_env={},
        )


def test_supported_provider_list_is_the_public_contract():
    assert SUPPORTED_PROVIDERS == ("anthropic", "openai", "ollama")


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "api_base_invalid"),
        ("example.com/v1", "api_base_invalid"),
        ("ftp://example.com/v1", "api_base_invalid"),
        ("https://user:pass@example.com/v1", "api_base_credentials"),
        ("https://example.com/v1?region=cn", "api_base_query_or_fragment"),
        ("https://example.com/v1#fragment", "api_base_query_or_fragment"),
        ("http://example.com/v1", "insecure_api_base"),
        ("https://example.com:bad/v1", "api_base_invalid"),
    ],
)
def test_validate_api_base_rejects_unsafe_values(value, reason):
    with pytest.raises(ValueError, match=f"^{reason}$"):
        validate_api_base(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:8080/v1",
        "http://127.0.0.1:8080/v1",
        "http://[::1]:8080/v1",
        "https://gateway.example/v1",
    ],
)
def test_validate_api_base_accepts_https_and_loopback_http(value):
    assert validate_api_base(value) == value


def test_legacy_provider_toml_section_is_not_a_runtime_config_source(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[provider]\nactive = 'legacy'\n",
        encoding="utf-8",
    )

    assert "provider" not in load_pico_toml(tmp_path)
