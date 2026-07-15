import pytest

import pico.config as config_module
from pico.config import (
    classify_provider_destination,
    load_pico_toml,
    resolve_provider_config,
)
from pico.recovery_policy import DEFAULT_MAX_BLOB_SIZE, snapshot_eligibility


def test_load_pico_toml_reads_simple_project_overrides(tmp_path):
    (tmp_path / "pico.toml").write_text(
        "[policy]\nmax_blob_size = 2048\n",
        encoding="utf-8",
    )

    assert load_pico_toml(tmp_path)["policy"]["max_blob_size"] == 2048


def test_project_max_blob_size_falls_back_to_default_when_missing(tmp_path):
    # 没有 pico.toml 时，project_max_blob_size 必须给出默认值，
    # 保证调用方可以无条件把返回值传给 snapshot_eligibility。
    assert load_pico_toml(tmp_path)["policy"]["max_blob_size"] == DEFAULT_MAX_BLOB_SIZE


def test_pico_toml_max_blob_size_overrides_snapshot_eligibility(tmp_path):
    # 一份 300 字节的文本文件：默认阈值下 eligible；把上限压到 100 后变 ineligible。
    file_rel = "notes/large.md"
    file_abs = tmp_path / file_rel
    file_abs.parent.mkdir(parents=True, exist_ok=True)
    file_abs.write_text("x" * 300, encoding="utf-8")

    baseline = snapshot_eligibility(tmp_path, file_rel)
    assert baseline["snapshot_eligible"] is True

    (tmp_path / "pico.toml").write_text(
        "[policy]\nmax_blob_size = 100\n",
        encoding="utf-8",
    )

    override_limit = load_pico_toml(tmp_path)["policy"]["max_blob_size"]
    assert override_limit == 100
    tightened = snapshot_eligibility(tmp_path, file_rel, max_blob_size=override_limit)
    assert tightened["snapshot_eligible"] is False
    assert tightened["ineligible_reason"] == "file_too_large"


def test_provider_resolver_uses_source_then_provider_name_order():
    resolved = resolve_provider_config(
        explicit={"provider": "openai"},
        project_env={
            "PICO_API_KEY": "project-shared",
            "OPENAI_MODEL": "project-model",
        },
        process_env={
            "PICO_OPENAI_API_KEY": "process-specific",
            "PICO_OPENAI_MODEL": "process-model",
        },
    )

    assert resolved["provider"] == {
        "value": "openai",
        "source": "cli",
        "name": "--provider",
    }
    assert resolved["api_key"] == {
        "value": "project-shared",
        "source": "project_env",
        "name": "PICO_API_KEY",
    }
    assert resolved["model"] == {
        "value": "project-model",
        "source": "project_env",
        "name": "OPENAI_MODEL",
    }


def test_provider_resolver_explicit_values_override_both_env_sources():
    resolved = resolve_provider_config(
        explicit={
            "provider": "anthropic",
            "model": "explicit-model",
            "base_url": "https://explicit.example/v1",
            "api_key": "explicit-key",
        },
        project_env={
            "PICO_PROVIDER": "openai",
            "PICO_ANTHROPIC_MODEL": "project-model",
            "PICO_ANTHROPIC_API_KEY": "project-key",
        },
        process_env={
            "PICO_PROVIDER": "deepseek",
            "PICO_ANTHROPIC_MODEL": "process-model",
            "PICO_ANTHROPIC_API_KEY": "process-key",
        },
    )

    assert {key: item["source"] for key, item in resolved.items()} == {
        "provider": "cli",
        "model": "cli",
        "base_url": "cli",
        "destination": "cli",
        "api_key": "cli",
    }
    assert resolved["model"]["value"] == "explicit-model"
    assert resolved["base_url"]["name"] == "--base-url"


@pytest.mark.parametrize(
    ("provider", "expected_url", "expected_class", "expected_host"),
    [
        ("openai", "https://api.openai.com/v1", "official", "api.openai.com"),
        ("anthropic", "https://api.anthropic.com", "official", "api.anthropic.com"),
        (
            "deepseek",
            "https://api.deepseek.com/anthropic",
            "official",
            "api.deepseek.com",
        ),
        ("ollama", "http://127.0.0.1:11434", "local", "127.0.0.1"),
    ],
)
def test_provider_defaults_have_safe_destination_classification(
    provider,
    expected_url,
    expected_class,
    expected_host,
):
    resolved = resolve_provider_config(
        explicit={"provider": provider},
        project_env={},
        process_env={},
    )

    assert resolved["base_url"]["value"] == expected_url
    assert resolved["destination"] == {
        "classification": expected_class,
        "host": expected_host,
        "source": "default",
        "name": (
            "DEFAULT_OLLAMA_HOST"
            if provider == "ollama"
            else f"DEFAULT_{provider.upper()}_BASE_URL"
        ),
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://relay.example/v1",
        "https://www.right.codes/codex/v1",
        "https://gateway.corp.example/provider",
    ],
)
def test_all_explicit_third_party_destinations_follow_one_rule(url):
    result = classify_provider_destination("openai", url, source="cli")

    assert result == {
        "classification": "explicit_third_party",
        "host": url.split("/")[2],
        "source": "cli",
    }


@pytest.mark.parametrize(
    ("kwargs", "expected_source"),
    [
        ({"explicit": {"base_url": "https://relay.example/v1"}}, "cli"),
        (
            {"project_env": {"PICO_OPENAI_API_BASE": "https://relay.example/v1"}},
            "project_env",
        ),
        (
            {"process_env": {"OPENAI_API_BASE": "https://relay.example/v1"}},
            "environment",
        ),
    ],
)
def test_resolver_allows_third_party_only_from_explicit_sources(
    kwargs,
    expected_source,
):
    explicit = {"provider": "openai", **kwargs.get("explicit", {})}
    resolved = resolve_provider_config(
        explicit=explicit,
        project_env=kwargs.get("project_env", {}),
        process_env=kwargs.get("process_env", {}),
    )

    assert resolved["destination"]["classification"] == "explicit_third_party"
    assert resolved["destination"]["source"] == expected_source


def test_implicit_third_party_default_fails_closed(monkeypatch):
    monkeypatch.setitem(
        config_module.DEFAULT_BASE_URLS,
        "openai",
        "https://relay.example/v1",
    )

    with pytest.raises(
        ValueError,
        match="provider_destination_implicit_third_party",
    ):
        resolve_provider_config(
            explicit={"provider": "openai"},
            project_env={},
            process_env={},
        )


@pytest.mark.parametrize(
    ("provider", "key_name", "expected_url"),
    [
        ("openai", "OPENAI_API_KEY", "https://api.openai.com/v1"),
        ("anthropic", "ANTHROPIC_API_KEY", "https://api.anthropic.com"),
    ],
)
def test_standard_provider_key_without_base_url_uses_official_destination(
    provider,
    key_name,
    expected_url,
):
    resolved = resolve_provider_config(
        explicit={"provider": provider},
        project_env={},
        process_env={key_name: "secret-value"},
    )

    assert resolved["base_url"]["value"] == expected_url
    assert resolved["destination"]["classification"] == "official"


@pytest.mark.parametrize(
    ("provider", "foreign_env"),
    [
        ("openai", {"PICO_ANTHROPIC_API_KEY": "foreign"}),
        ("anthropic", {"OPENAI_API_KEY": "foreign"}),
        ("deepseek", {"ANTHROPIC_API_KEY": "foreign"}),
        ("ollama", {"PICO_API_KEY": "foreign"}),
    ],
)
def test_provider_resolver_rejects_cross_provider_keys(provider, foreign_env):
    resolved = resolve_provider_config(
        explicit={"provider": provider},
        project_env=foreign_env,
        process_env={},
    )

    assert resolved["api_key"] == {"value": "", "source": "unset", "name": ""}


@pytest.mark.parametrize("provider", ["openai", "anthropic", "deepseek"])
def test_provider_resolver_uses_shared_api_key_last_within_a_source(provider):
    resolved = resolve_provider_config(
        explicit={"provider": provider},
        project_env={"PICO_API_KEY": "shared"},
        process_env={},
    )

    assert resolved["api_key"] == {
        "value": "shared",
        "source": "project_env",
        "name": "PICO_API_KEY",
    }
