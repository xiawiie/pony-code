import pytest

from pico.security import (
    REDACTED_VALUE,
    contains_secret_material,
    detected_secret_env_items,
    looks_secret_shaped_text,
    looks_sensitive_env_name,
    redact_artifact,
    redact_text,
    shell_env,
)

SECRET_SENTINEL = "github_pat_A123456789012345678901234567890"


def test_sensitive_env_name_detection_matches_runtime_policy():
    assert looks_sensitive_env_name("OPENAI_API_KEY")
    assert looks_sensitive_env_name("SERVICE_TOKEN")
    assert looks_sensitive_env_name("PASSWORD")
    assert not looks_sensitive_env_name("PATH")


def test_detected_secret_env_items_include_configured_and_sensitive_names():
    env = {
        "PATH": "/bin",
        "CUSTOM_SECRET_NAME": "custom-value",
        "OPENAI_API_KEY": "api-value",
    }

    items = detected_secret_env_items(env=env, secret_env_names={"CUSTOM_SECRET_NAME"})

    assert items == [("CUSTOM_SECRET_NAME", "custom-value"), ("OPENAI_API_KEY", "api-value")]


def test_redact_artifact_recurses_through_values_and_secret_keys():
    artifact = {
        "OPENAI_API_KEY": "api-value",
        "payload": ["api-value", {"nested": "custom-value"}],
    }

    redacted = redact_artifact(
        artifact,
        env={"OPENAI_API_KEY": "api-value", "CUSTOM_SECRET_NAME": "custom-value"},
        secret_env_names={"CUSTOM_SECRET_NAME"},
    )

    assert redacted["OPENAI_API_KEY"] == REDACTED_VALUE
    assert redacted["payload"] == [REDACTED_VALUE, {"nested": REDACTED_VALUE}]


def test_common_token_families_are_secret_shaped():
    assert looks_secret_shaped_text("Deploy credential ghp_1234567890abcdefghijklmnopqrstuv")
    assert looks_secret_shaped_text("AWS access id AKIA1234567890ABCDEF")
    assert looks_secret_shaped_text("Slack value xoxb-123456789012-abcdefghijklmnop")


def test_short_secret_values_do_not_redact_broad_substrings():
    env = {"OPENAI_API_KEY": "abc"}

    assert redact_text("abc", env=env) == REDACTED_VALUE
    assert redact_text("The abc suffix appears in non-secret prose.", env=env) == (
        "The abc suffix appears in non-secret prose."
    )
    assert redact_artifact({"OPENAI_API_KEY": "abc"}, env=env)["OPENAI_API_KEY"] == REDACTED_VALUE


def test_long_secret_values_redact_token_instances_including_embedded_text():
    env = {"OPENAI_API_KEY": "alpha123456789"}

    assert redact_text("token=alpha123456789", env=env) == f"token={REDACTED_VALUE}"
    assert redact_text("Use alpha123456789 for the request.", env=env) == (
        f"Use {REDACTED_VALUE} for the request."
    )
    assert redact_text("identifier_alpha123456789_suffix", env=env) == f"identifier_{REDACTED_VALUE}_suffix"


def test_redact_text_removes_known_secret_even_inside_identifier():
    env = {"PICO_API_KEY": "alpha123456789"}
    assert redact_text("prefix_alpha123456789_suffix", env=env) == "prefix_<redacted>_suffix"


def test_redact_text_covers_high_confidence_material_without_env():
    samples = [
        SECRET_SENTINEL,
        "Authorization: Bearer bearer-secret-123456789",
        "password=correct-horse-battery-staple",
        "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----",
        "https://user:secret-pass@example.test/v1?api_key=alpha123456789",
        "https://user:secret-pass@example.test/v1",
        "tool --api-key sk-cli-123456789",
    ]
    for sample in samples:
        safe = redact_text(sample, env={})
        assert sample not in safe
        assert not contains_secret_material(safe, env={})


def test_secret_detector_ignores_security_prose_and_sample_values():
    for text in (
        "token budget",
        "password policy",
        "credential rotation design",
        "input_tokens",
        "API_KEY=your-api-key",
        "TOKEN=${TOKEN}",
    ):
        assert contains_secret_material(text, env={}) is False
        assert redact_text(text, env={}) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ('password="correct horse battery staple"', f'password="{REDACTED_VALUE}"'),
        ("password='correct;horse,battery staple'", f"password='{REDACTED_VALUE}'"),
        ('tool --password "correct horse battery staple"', f'tool --password "{REDACTED_VALUE}"'),
        ("tool --password='correct;horse,battery staple'", f"tool --password='{REDACTED_VALUE}'"),
    ),
)
def test_redact_text_consumes_complete_quoted_assignment_and_flag_values(text, expected):
    assert redact_text(text, env={}) == expected
    assert contains_secret_material(expected, env={}) is False


@pytest.mark.parametrize(
    "text",
    (
        "Authorization: Bearer example",
        "tool --api-key ${TOKEN}",
        "https://user:<placeholder>@example.test/v1",
        "https://example.test/v1?api_key=your-api-key",
        "API_KEY=<sk-example>",
    ),
)
def test_placeholder_values_are_preserved_across_all_redaction_stages(text):
    assert redact_text(text, env={}) == text
    assert contains_secret_material(text, env={}) is False


def test_redact_text_preserves_marker_when_env_secret_overlaps_it():
    env = {"TOKEN": "redacted"}

    safe = redact_text("redacted", env=env)

    assert safe == REDACTED_VALUE
    assert redact_text(safe, env=env) == safe
    assert contains_secret_material(REDACTED_VALUE, env=env) is False


def test_known_env_secret_containing_marker_is_replaced_as_one_value():
    env = {"TOKEN": "prefix<redacted>suffix"}

    safe = redact_text("prefix<redacted>suffix", env=env)

    assert safe == REDACTED_VALUE
    assert contains_secret_material("prefix<redacted>suffix", env=env) is True
    assert contains_secret_material(safe, env=env) is False


def test_concrete_token_inside_markup_is_redacted():
    text = '<meta content="sk-real-secret-123456">'
    safe = redact_text(text, env={})

    assert safe == f'<meta content="{REDACTED_VALUE}">'
    assert contains_secret_material(text, env={}) is True
    assert contains_secret_material(safe, env={}) is False


@pytest.mark.parametrize(
    "key",
    (
        "api_key",
        "access_key",
        "auth_token",
        "bearer_token",
        "credential",
        "credentials",
        "client_secret",
        "password",
        "token",
        "authorization",
        "private_key",
    ),
)
def test_redact_artifact_replaces_opaque_values_for_secret_mapping_keys(key):
    value = "opaque-value-with-no-token-shape"
    assert redact_artifact({key: value}, env={}) == {key: REDACTED_VALUE}


def test_redact_artifact_preserves_non_secret_metric_keys():
    value = {"input_tokens": 12, "token_budget": 2048, "credential_policy": "rotate quarterly"}
    assert redact_artifact(value, env={}) == value


@pytest.mark.parametrize(
    "text",
    (
        '{"api_key":"opaque-json-value"}',
        '{"apiKey":"opaque-json-value"}',
        '{"clientSecret":"opaque-json-value"}',
        '{"accessToken":"opaque-json-value"}',
    ),
)
def test_quoted_json_secret_assignment_is_detected_and_redacted(text):
    safe = redact_text(text, env={})
    assert "opaque-json-value" not in safe
    assert contains_secret_material(text, env={}) is True


@pytest.mark.parametrize("key", ("apiKey", "clientSecret", "accessToken", "privateKey"))
def test_camel_case_secret_mapping_key_is_redacted(key):
    assert redact_artifact({key: "opaque-value"}, env={}) == {key: REDACTED_VALUE}


def test_shell_env_uses_allowlist_and_sets_pwd_with_path_fallback(tmp_path):
    env = {"PATH": "/usr/bin", "HOME": "/home/user", "SECRET": "nope"}

    filtered = shell_env(env=env, allowlist=("HOME",), root=tmp_path)

    assert filtered == {"HOME": "/home/user", "PWD": str(tmp_path), "PATH": "/usr/bin"}
