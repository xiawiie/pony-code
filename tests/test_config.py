import pytest

from pico.config import (
    API_KEY_ENV_NAME,
    API_URL_ENV_NAME,
    DEFAULT_API_URL,
    DEFAULT_MODEL,
    load_pico_toml,
    resolve_model_config,
    validate_api_url,
)
from pico.recovery_policy import DEFAULT_MAX_BLOB_SIZE, snapshot_eligibility


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


def test_model_config_uses_fixed_contract_and_default_url():
    resolved = resolve_model_config(
        project_env={API_KEY_ENV_NAME: "project-key"},
        process_env={},
    )

    assert resolved["model"] == {
        "value": DEFAULT_MODEL,
        "source": "fixed",
        "name": "DEFAULT_MODEL",
    }
    assert resolved["protocol"]["value"] == "openai_chat_completions"
    assert resolved["auth_mode"]["value"] == "bearer"
    assert resolved["base_url"]["value"] == DEFAULT_API_URL


def test_project_env_wins_over_process_env_for_url_and_key():
    resolved = resolve_model_config(
        project_env={
            API_URL_ENV_NAME: "https://project.example/v1",
            API_KEY_ENV_NAME: "project-key",
        },
        process_env={
            API_URL_ENV_NAME: "https://process.example/v1",
            API_KEY_ENV_NAME: "process-key",
        },
    )

    assert resolved["base_url"] == {
        "value": "https://project.example/v1",
        "source": "project_env",
        "name": API_URL_ENV_NAME,
    }
    assert resolved["api_key"] == {
        "value": "project-key",
        "source": "project_env",
        "name": API_KEY_ENV_NAME,
    }


def test_process_env_is_used_when_project_values_are_unset():
    resolved = resolve_model_config(
        project_env={API_URL_ENV_NAME: "", API_KEY_ENV_NAME: ""},
        process_env={
            API_URL_ENV_NAME: "https://process.example/v1/",
            API_KEY_ENV_NAME: "process-key",
        },
    )

    assert resolved["base_url"]["value"] == "https://process.example/v1"
    assert resolved["api_key"]["value"] == "process-key"


def test_only_legacy_or_vendor_variables_cannot_configure_runtime():
    legacy = {
        "PICO_PROVIDER": "anthropic",
        "PICO_PROFILE": "deepseek",
        "PICO_CONNECTION": "old",
        "PICO_DEEPSEEK_API_BASE": "https://legacy.example/v1",
        "PICO_DEEPSEEK_MODEL": "legacy-model",
        "OPENAI_API_KEY": "openai-key",
        "ANTHROPIC_API_KEY": "anthropic-key",
        "PICO_API_KEY": "shared-key",
    }

    with pytest.raises(ValueError, match="^api_key_not_configured$"):
        resolve_model_config(project_env=legacy, process_env={})

    inspected = resolve_model_config(
        project_env=legacy,
        process_env={},
        required=False,
    )
    assert inspected["base_url"]["value"] == DEFAULT_API_URL
    assert inspected["api_key"] == {"value": "", "source": "unset", "name": ""}


def test_missing_key_is_allowed_only_for_read_only_diagnostics():
    with pytest.raises(ValueError, match="^api_key_not_configured$"):
        resolve_model_config(project_env={}, process_env={})

    assert resolve_model_config(
        project_env={}, process_env={}, required=False
    )["api_key"]["value"] == ""


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "api_url_invalid"),
        ("example.com/v1", "api_url_invalid"),
        ("ftp://example.com/v1", "api_url_invalid"),
        ("https://user:pass@example.com/v1", "api_url_credentials"),
        ("https://example.com/v1?region=cn", "api_url_query_or_fragment"),
        ("https://example.com/v1#fragment", "api_url_query_or_fragment"),
        ("http://example.com/v1", "insecure_api_url"),
        ("https://example.com:bad/v1", "api_url_invalid"),
    ],
)
def test_validate_api_url_rejects_unsafe_values(value, reason):
    with pytest.raises(ValueError, match=f"^{reason}$"):
        validate_api_url(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:8080/v1",
        "http://127.0.0.1:8080/v1",
        "http://[::1]:8080/v1",
        "https://gateway.example/v1",
    ],
)
def test_validate_api_url_accepts_https_and_loopback_http(value):
    assert validate_api_url(value) == value


def test_legacy_provider_toml_section_is_not_a_runtime_config_source(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[provider]\nactive = 'legacy'\n",
        encoding="utf-8",
    )

    assert "provider" not in load_pico_toml(tmp_path)
