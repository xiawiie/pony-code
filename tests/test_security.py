from pico.security import (
    REDACTED_VALUE,
    detected_secret_env_items,
    looks_secret_shaped_text,
    looks_sensitive_env_name,
    redact_artifact,
    redact_text,
    shell_env,
)


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


def test_shell_env_uses_allowlist_and_sets_pwd_with_path_fallback(tmp_path):
    env = {"PATH": "/usr/bin", "HOME": "/home/user", "SECRET": "nope"}

    filtered = shell_env(env=env, allowlist=("HOME",), root=tmp_path)

    assert filtered == {"HOME": "/home/user", "PWD": str(tmp_path), "PATH": "/usr/bin"}
