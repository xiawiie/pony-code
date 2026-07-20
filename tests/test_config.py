import hashlib
import os

import pytest

from pony.config.model import (
    API_BASE_ENV_NAME,
    API_KEY_ENV_NAME,
    DEFAULT_API_BASE,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    MODEL_ENV_NAME,
    PROVIDER_ENV_NAME,
    SUPPORTED_PROVIDERS,
    resolve_model_config,
    resolve_provider_candidate,
    resolve_session_provider_binding,
    validate_api_base,
)
from pony.config.project import load_pony_toml


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo", "directory"))
def test_pony_toml_unsafe_entry_warns_and_uses_defaults(
    tmp_path,
    capsys,
    kind,
):
    outside = tmp_path / "outside.toml"
    outside.write_text("[model]\noutput_limit = 1\n", encoding="utf-8")
    target = tmp_path / "pony.toml"
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        sibling = tmp_path.parent / f"{tmp_path.name}-outside.toml"
        sibling.write_text("[model]\noutput_limit = 1\n", encoding="utf-8")
        os.link(sibling, target)
    elif kind == "fifo":
        os.mkfifo(target, 0o600)
    else:
        target.mkdir()

    config = load_pony_toml(tmp_path)

    assert config["model"]["output_limit"] == 16_384
    assert capsys.readouterr().err == "warning: invalid pony.toml; using defaults\n"


def test_pony_toml_over_one_mib_warns_and_uses_defaults(tmp_path, capsys):
    (tmp_path / "pony.toml").write_bytes(b"#" * (1024 * 1024 + 1))

    config = load_pony_toml(tmp_path)

    assert config["model"]["output_limit"] == 16_384
    assert capsys.readouterr().err == "warning: invalid pony.toml; using defaults\n"


def test_diagnostics_do_not_invent_provider_defaults():
    resolved = resolve_model_config(
        project_env={},
        process_env={},
        required=False,
    )

    assert resolved["provider"]["value"] == DEFAULT_PROVIDER
    assert resolved["model"]["value"] == DEFAULT_MODEL
    assert resolved["protocol"]["value"] == ""
    assert resolved["api_variant"]["value"] == ""
    assert resolved["auth_mode"]["value"] == ""
    assert resolved["base_url"]["value"] == DEFAULT_API_BASE
    assert resolved["resolution_status"] == "invalid"
    assert resolved["resolution_source"] == ""
    assert resolved["resolution_error"] == "api_base_not_configured"
    assert resolved["candidates"] == []


def test_diagnostics_with_only_key_remain_unresolved():
    resolved = resolve_model_config(
        project_env={API_KEY_ENV_NAME: "project-key"},
        process_env={},
        required=False,
    )

    assert resolved["base_url"] == {"value": "", "source": "unset", "name": ""}
    assert resolved["resolution_status"] == "invalid"


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
            "openai-chat",
            "openai_chat_completions",
            "chat_completions",
            "bearer",
            "gpt-5.4",
        ),
        (
            "https://gateway.example/v1",
            "openai-responses",
            "openai_responses",
            "responses",
            "bearer",
            "gpt-5.4",
        ),
    ],
)
def test_api_base_resolves_the_transport(
    api_base, provider, protocol, variant, auth_mode, model
):
    env = {PROVIDER_ENV_NAME: provider, API_BASE_ENV_NAME: api_base}
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
    assert resolved["resolution_status"] == "resolved"
    assert resolved["resolution_source"] in {"explicit", "known_origin"}


@pytest.mark.parametrize(
    "api_base",
    ("HTTPS://api.openai.com/v1", "https://API.OPENAI.COM:443/v1/"),
)
def test_official_api_base_is_canonicalized_before_capability_binding(api_base):
    resolved = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: "openai-responses",
            API_BASE_ENV_NAME: api_base,
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
        required=False,
    )

    assert resolved["base_url"]["value"] == "https://api.openai.com/v1"
    assert resolved["capabilities"] == {
        "strict_tools": True,
        "parallel_tool_control": True,
        "reasoning_replay": True,
    }


def test_openai_family_requires_probe_for_a_generic_endpoint():
    resolved = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: "openai",
            API_BASE_ENV_NAME: "https://gateway.example/v1",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
        required=False,
    )

    assert resolved["resolution_status"] == "probe_required"
    assert resolved["resolution_source"] == ""
    assert resolved["protocol"]["value"] == ""
    assert resolved["auth_mode"]["value"] == "bearer"
    assert resolved["capabilities"] == {}
    assert [candidate["protocol"] for candidate in resolved["candidates"]] == [
        "openai_chat_completions",
        "openai_responses",
    ]
    assert all(not candidate["capabilities"] for candidate in resolved["candidates"])


@pytest.mark.parametrize("provider", (None, "", "auto"))
def test_auto_provider_uses_bounded_generic_https_candidates(provider):
    env = {
        API_BASE_ENV_NAME: "https://gateway.example/v1",
        MODEL_ENV_NAME: "gateway-model",
        API_KEY_ENV_NAME: "test-key",
    }
    if provider is not None:
        env[PROVIDER_ENV_NAME] = provider

    resolved = resolve_model_config(project_env=env, process_env={})

    assert resolved["provider"]["value"] == "auto"
    assert resolved["resolution_status"] == "probe_required"
    assert [candidate["protocol"] for candidate in resolved["candidates"]] == [
        "openai_chat_completions",
        "openai_responses",
    ]


def test_auto_provider_prefers_ollama_for_an_unknown_loopback_endpoint():
    resolved = resolve_model_config(
        project_env={
            API_BASE_ENV_NAME: "http://127.0.0.1:8080/v1",
            MODEL_ENV_NAME: "local-model",
        },
        process_env={},
    )

    assert resolved["resolution_status"] == "probe_required"
    assert [candidate["protocol"] for candidate in resolved["candidates"]] == [
        "ollama_chat",
        "openai_chat_completions",
        "openai_responses",
    ]


@pytest.mark.parametrize(
    ("api_base", "protocol", "resolved_provider"),
    [
        ("https://api.openai.com/v1", "openai_responses", "openai-responses"),
        ("https://api.anthropic.com/v1", "anthropic_messages", "anthropic"),
        ("http://localhost:11434", "ollama_chat", "ollama"),
    ],
)
def test_auto_provider_resolves_known_origins(api_base, protocol, resolved_provider):
    resolved = resolve_model_config(
        project_env={
            API_BASE_ENV_NAME: api_base,
            MODEL_ENV_NAME: "known-model",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
    )

    assert resolved["provider"]["value"] == "auto"
    assert resolved["resolved_provider"]["value"] == resolved_provider
    assert resolved["protocol"]["value"] == protocol
    assert resolved["resolution_status"] == "resolved"
    assert resolved["resolution_source"] == "known_origin"


def test_generic_forced_protocol_has_no_unverified_optional_capabilities():
    generic = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: "openai-responses",
            API_BASE_ENV_NAME: "https://gateway.example/v1",
            MODEL_ENV_NAME: "gateway-model",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
    )
    official = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: "openai-responses",
            API_BASE_ENV_NAME: "https://api.openai.com/v1",
            MODEL_ENV_NAME: "gpt-test",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
    )
    noncanonical_official_path = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: "openai-responses",
            API_BASE_ENV_NAME: "https://api.openai.com/compatible/v1",
            MODEL_ENV_NAME: "gpt-test",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
    )

    assert generic["capabilities"] == {}
    assert noncanonical_official_path["capabilities"] == {}
    assert official["capabilities"] == {
        "strict_tools": True,
        "parallel_tool_control": True,
        "reasoning_replay": True,
    }


def test_probe_candidate_projects_a_complete_resolved_config():
    unresolved = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: "openai",
            API_BASE_ENV_NAME: "https://gateway.example/v1",
            MODEL_ENV_NAME: "gateway-model",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
    )

    resolved = resolve_provider_candidate(unresolved, "openai_chat_completions")

    assert resolved["resolution_status"] == "resolved"
    assert resolved["resolution_source"] == "probe"
    assert resolved["resolved_provider"]["value"] == "openai-chat"
    assert resolved["protocol"]["value"] == "openai_chat_completions"
    assert resolved["auth_mode"]["value"] == "bearer"
    assert resolved["capabilities"] == {}
    assert resolved["candidates"] == []


def test_project_env_wins_over_process_env_for_all_three_fields():
    resolved = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: "openai",
            MODEL_ENV_NAME: "project-model",
            API_BASE_ENV_NAME: "https://project.example/v1",
            API_KEY_ENV_NAME: "project-key",
        },
        process_env={
            PROVIDER_ENV_NAME: "anthropic",
            MODEL_ENV_NAME: "process-model",
            API_BASE_ENV_NAME: "https://api.anthropic.com/v1",
            API_KEY_ENV_NAME: "process-key",
        },
    )

    assert resolved["provider"]["value"] == "openai"
    assert resolved["model"]["value"] == "project-model"
    assert resolved["protocol"]["value"] == ""
    assert resolved["resolution_status"] == "probe_required"
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
            PROVIDER_ENV_NAME: "openai",
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
        "PONY_PROFILE": "deepseek",
        "PONY_CONNECTION": "old",
        "PONY_DEEPSEEK_API_BASE": "https://legacy.example/v1",
        "PONY_DEEPSEEK_MODEL": "legacy-model",
        "PONY_DEEPSEEK_API_KEY": "deepseek-key",
        "OPENAI_API_KEY": "openai-key",
        "ANTHROPIC_API_KEY": "anthropic-key",
        "PONY_PROVIDER": "anthropic",
        "PONY_API_URL": "https://api.anthropic.com/v1",
        "PONY_API_VARIANT": "messages",
        "PONY_AUTH_MODE": "x-api-key",
    }

    with pytest.raises(ValueError, match="^api_base_not_configured$"):
        resolve_model_config(project_env=legacy, process_env={})

    inspected = resolve_model_config(
        project_env=legacy,
        process_env={},
        required=False,
    )
    assert inspected["base_url"] == {
        "value": "https://api.anthropic.com/v1",
        "source": "default",
        "name": "anthropic_default_api_base",
    }
    assert inspected["api_key"] == {"value": "", "source": "unset", "name": ""}


def test_missing_connection_is_invalid_for_diagnostics_and_rejected_at_runtime():
    with pytest.raises(ValueError, match="^api_base_not_configured$"):
        resolve_model_config(project_env={}, process_env={})

    inspected = resolve_model_config(project_env={}, process_env={}, required=False)
    assert inspected["api_key"]["value"] == ""
    assert inspected["resolution_status"] == "invalid"


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
        PROVIDER_ENV_NAME: "anthropic",
        API_BASE_ENV_NAME: "https://api.anthropic.com/v1",
        MODEL_ENV_NAME: "claude-test",
        API_KEY_ENV_NAME: "test-key",
    }
    env.pop(missing_name)

    with pytest.raises(ValueError, match=f"^{reason}$"):
        resolve_model_config(project_env=env, process_env={})


def test_runtime_allows_provider_to_be_omitted():
    resolved = resolve_model_config(
        project_env={
            API_BASE_ENV_NAME: "https://api.anthropic.com/v1",
            MODEL_ENV_NAME: "claude-test",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
    )

    assert resolved["provider"]["value"] == "auto"
    assert resolved["protocol"]["value"] == "anthropic_messages"


def test_ollama_does_not_require_an_api_key():
    resolved = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: "ollama",
            API_BASE_ENV_NAME: "http://127.0.0.1:11434",
            MODEL_ENV_NAME: "qwen3:8b",
        },
        process_env={},
    )

    assert resolved["auth_mode"]["value"] == "none"
    assert resolved["api_key"]["value"] == ""


@pytest.mark.parametrize(
    ("provider", "api_base"),
    [
        ("anthropic", "https://api.anthropic.com/v1"),
        ("openai", "https://api.openai.com/v1"),
    ],
)
def test_cloud_providers_require_key(provider, api_base):
    with pytest.raises(ValueError, match="^api_key_not_configured$"):
        resolve_model_config(
            project_env={
                PROVIDER_ENV_NAME: provider,
                API_BASE_ENV_NAME: api_base,
                MODEL_ENV_NAME: "cloud-test-model",
            },
            process_env={},
        )


def test_supported_provider_list_is_the_public_contract():
    assert SUPPORTED_PROVIDERS == (
        "auto",
        "openai",
        "openai-chat",
        "openai-responses",
        "anthropic",
        "ollama",
    )


def test_explicit_provider_rejects_a_conflicting_known_origin():
    with pytest.raises(ValueError, match="^provider_endpoint_conflict$"):
        resolve_model_config(
            project_env={
                PROVIDER_ENV_NAME: "anthropic",
                API_BASE_ENV_NAME: "https://api.openai.com/v1",
                MODEL_ENV_NAME: "claude-test",
                API_KEY_ENV_NAME: "test-key",
            },
            process_env={},
        )


def test_compatible_session_binding_resolves_without_probe():
    config = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: "openai",
            API_BASE_ENV_NAME: "https://gateway.example/v1",
            MODEL_ENV_NAME: "gateway-model",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
    )
    endpoint_hash = "sha256:" + hashlib.sha256(
        b"https://gateway.example/v1"
    ).hexdigest()

    resolved = resolve_session_provider_binding(
        config,
        {
            "protocol_family": "openai_chat_completions",
            "model": "gateway-model",
            "endpoint_hash": endpoint_hash,
        },
    )

    assert resolved["resolution_status"] == "resolved"
    assert resolved["resolution_source"] == "session_binding"
    assert resolved["protocol"]["value"] == "openai_chat_completions"
    assert resolved["capabilities"] == {}


def test_generic_auto_session_rejects_legacy_anthropic_binding():
    config = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: "auto",
            API_BASE_ENV_NAME: "https://gateway.example/v1",
            MODEL_ENV_NAME: "gateway-model",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
    )

    with pytest.raises(ValueError, match="^model_session_mismatch$"):
        resolve_session_provider_binding(
            config,
            {
                "protocol_family": "anthropic_messages",
                "model": "gateway-model",
                "endpoint_hash": "sha256:"
                + hashlib.sha256(b"https://gateway.example/v1").hexdigest(),
            },
        )


@pytest.mark.parametrize(
    ("provider", "api_base", "protocol"),
    [
        ("auto", "https://gateway.example/v1", "openai_responses"),
        ("auto", "http://127.0.0.1:8080/v1", "ollama_chat"),
        ("anthropic", "https://gateway.example/v1", "anthropic_messages"),
        ("auto", "https://api.anthropic.com/v1", "anthropic_messages"),
    ],
)
def test_session_binding_accepts_current_provider_candidates(
    provider,
    api_base,
    protocol,
):
    config = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: provider,
            API_BASE_ENV_NAME: api_base,
            MODEL_ENV_NAME: "gateway-model",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
    )

    resolved = resolve_session_provider_binding(
        config,
        {
            "protocol_family": protocol,
            "model": "gateway-model",
            "endpoint_hash": "sha256:" + hashlib.sha256(api_base.encode()).hexdigest(),
        },
    )

    assert resolved["protocol"]["value"] == protocol
    assert resolved["resolution_source"] == "session_binding"


@pytest.mark.parametrize("field", ("protocol_family", "endpoint_hash"))
def test_incompatible_session_binding_fails_closed(field):
    config = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: "openai",
            API_BASE_ENV_NAME: "https://gateway.example/v1",
            MODEL_ENV_NAME: "gateway-model",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
    )
    binding = {
        "protocol_family": "openai_chat_completions",
        "model": "gateway-model",
        "endpoint_hash": "sha256:"
        + hashlib.sha256(b"https://gateway.example/v1").hexdigest(),
    }
    binding[field] = {
        "protocol_family": "anthropic_messages",
        "model": "other-model",
        "endpoint_hash": "sha256:" + "a" * 64,
    }[field]

    with pytest.raises(ValueError, match="^model_session_mismatch$"):
        resolve_session_provider_binding(config, binding)


def test_session_binding_model_overrides_environment_without_probe():
    config = resolve_model_config(
        project_env={
            PROVIDER_ENV_NAME: "openai",
            API_BASE_ENV_NAME: "https://gateway.example/v1",
            MODEL_ENV_NAME: "configured-model",
            API_KEY_ENV_NAME: "test-key",
        },
        process_env={},
    )
    binding = {
        "protocol_family": "openai_chat_completions",
        "model": "session-model",
        "endpoint_hash": "sha256:"
        + hashlib.sha256(b"https://gateway.example/v1").hexdigest(),
    }

    resolved = resolve_session_provider_binding(config, binding)

    assert resolved["model"] == {
        "value": "session-model",
        "source": "session_binding",
        "name": "",
    }


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
    (tmp_path / "pony.toml").write_text(
        "[provider]\nactive = 'legacy'\n",
        encoding="utf-8",
    )

    assert "provider" not in load_pony_toml(tmp_path)
