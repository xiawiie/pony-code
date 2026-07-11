import os
import stat

import pytest

from pico import security as security_module
from pico.security import (
    REDACTED_VALUE,
    contains_secret_material,
    detected_secret_env_items,
    ensure_private_file,
    has_sensitive_path_suffix,
    is_sensitive_path,
    looks_secret_shaped_text,
    looks_sensitive_env_name,
    redact_artifact,
    redact_text,
    sensitive_path_reason,
    shell_env,
)

SECRET_SENTINEL = "github_pat_A123456789012345678901234567890"


@pytest.mark.parametrize(
    "path",
    (
        ".env",
        ".env.local",
        ".envrc",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "config/credentials.json",
        "config/auth.json",
        "config/service-account.json",
        "config/service-account-prod.json",
        "config/secrets.json",
        "config/secrets.yaml",
        "config/secrets.yml",
        "config/secrets.toml",
        ".ssh",
        ".ssh/id_ed25519",
        ".gnupg/private-keys-v1.d/key",
        ".aws/credentials",
        ".docker/config.json",
        ".kube/config",
        "certs/client.pem",
        "keys/signing.key",
        "keys/client.p12",
        "keys/client.pfx",
        "keys/client.jks",
        "keys/client.keystore",
        ".pico/sessions",
        ".pico/sessions/s.json",
        ".pico/runs/r/trace.jsonl",
        ".pico/checkpoints/blobs/aa/value",
    ),
)
def test_sensitive_path_matrix(path):
    assert is_sensitive_path(path)
    assert sensitive_path_reason(path) == "sensitive_path"


@pytest.mark.parametrize(
    "path",
    (
        ".env.example",
        ".env.sample",
        ".env.template",
        "certs/ca.crt",
        "id_ed25519.pub",
        "config/service-account.txt",
        "config/secret.json",
        ".aws/config",
        ".pico/memory/agent_notes.md",
    ),
)
def test_sensitive_path_templates_and_public_material_are_allowed(path):
    assert not is_sensitive_path(path)
    assert sensitive_path_reason(path) == ""


@pytest.mark.parametrize(
    "path",
    (
        ".env/child.txt",
        ".env.local/child.txt",
        ".env.example/child.txt",
        "credentials.json/child.txt",
        "service-account-prod.json/child.txt",
        "client.pem/child.txt",
    ),
)
def test_sensitive_path_rules_apply_to_every_component(path):
    assert is_sensitive_path(path)
    assert sensitive_path_reason(path) == "sensitive_path"


@pytest.mark.parametrize(
    "path",
    (
        "./.ENV.LOCAL",
        "PROJECT/.SSH/ID_ED25519",
        "PROJECT/.PICO/CHECKPOINTS/record.json",
        "CERTS/CLIENT.PEM",
        "PROJECT\\.SSH\\ID_ED25519",
    ),
)
def test_sensitive_path_uses_casefolded_posix_lexical_normalization(path):
    assert is_sensitive_path(path)


@pytest.mark.parametrize(
    "path",
    (
        ".aws/tmp/../credentials",
        ".docker/tmp/../config.json",
        ".kube/tmp/../config",
        ".pico/tmp/../sessions/s.json",
    ),
)
def test_sensitive_path_collapses_lexical_parent_components(path):
    assert is_sensitive_path(path)


def test_sensitive_path_classification_does_not_read_or_resolve(tmp_path):
    ordinary = tmp_path / "README.md"
    ordinary.write_text(SECRET_SENTINEL, encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / ".env")

    assert not is_sensitive_path(ordinary)
    assert not is_sensitive_path(alias)


@pytest.mark.parametrize(
    "token",
    (
        "-sKcredentials.json",
        "--key=service-account-prod.json",
        "-Kclient.pem",
        "-K.env",
    ),
)
def test_sensitive_path_suffix_detects_option_attached_material(token):
    assert has_sensitive_path_suffix(token)


@pytest.mark.parametrize(
    "token",
    ("README.md", "-K.env.example", "config/secret.json"),
)
def test_sensitive_path_suffix_ignores_public_material(token):
    assert not has_sensitive_path_suffix(token)


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


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX descriptor traversal")
def test_private_file_open_rejects_parent_swap_before_leaf_open(tmp_path, monkeypatch):
    parent = tmp_path / "private"
    parent.mkdir()
    target = parent / "value.txt"
    target.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_target = outside / target.name
    outside_target.write_text("outside", encoding="utf-8")
    original_parent = tmp_path / "private-original"
    real_open = security_module.os.open
    swapped = False

    def swap_parent(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        candidate = os.fspath(path)
        if not swapped and (
            candidate == os.fspath(target)
            or (dir_fd is not None and candidate == target.name)
        ):
            parent.rename(original_parent)
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(security_module.os, "open", swap_parent)

    with pytest.raises(ValueError, match="changed|unsafe"):
        ensure_private_file(target)

    assert stat.S_IMODE(outside_target.stat().st_mode) == 0o644
    assert stat.S_IMODE((original_parent / target.name).stat().st_mode) == 0o644


def test_anchored_regular_reader_fifo_swap_is_nonblocking(tmp_path, monkeypatch):
    target = tmp_path / "note.txt"
    target.write_bytes(b"safe")
    real_open = os.open
    swapped = False

    def swap_to_fifo(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == target.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            assert flags & getattr(os, "O_NONBLOCK", 0)
            target.unlink()
            os.mkfifo(target, 0o600)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(security_module.os, "open", swap_to_fifo)

    with pytest.raises(ValueError, match="regular"):
        security_module.read_regular_bytes_anchored(
            tmp_path, "note.txt", max_bytes=1024
        )
    assert swapped is True


def test_anchored_regular_reader_stops_at_max_plus_one(tmp_path):
    (tmp_path / "large.txt").write_bytes(b"x" * 4096)
    state = security_module.read_regular_bytes_anchored(
        tmp_path, "large.txt", max_bytes=32
    )
    assert state["exists"] is True
    assert len(state["data"]) == 33
