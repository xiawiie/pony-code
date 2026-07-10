import os
import stat
from pathlib import Path

import pytest

from pico.cli import main
from pico.config import (
    load_project_env,
    project_env_path,
    read_project_env,
    write_project_env_assignments,
)


def test_project_env_never_falls_back_to_parent(tmp_path):
    parent = tmp_path / ".env"
    child = tmp_path / "repo"
    child.mkdir()
    parent.write_text("PICO_PROVIDER=anthropic\n", encoding="utf-8")

    assert project_env_path(child) == child.resolve() / ".env"
    assert read_project_env(child, warn=False) == {}


def test_secret_names_cannot_import_execution_control_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "PICO_SECRET_ENV_NAMES=PATH,PYTHONPATH\n"
        "PATH=./fake\n"
        "PYTHONPATH=./payload\n"
        "PICO_PROVIDER=deepseek\n",
        encoding="utf-8",
    )
    original_path = os.environ.get("PATH")
    monkeypatch.delenv("PYTHONPATH", raising=False)

    loaded = load_project_env(tmp_path)

    assert loaded["PICO_PROVIDER"] == "deepseek"
    assert os.environ.get("PATH") == original_path
    assert "PYTHONPATH" not in os.environ


@pytest.mark.parametrize("value", (" a # b ", "quote'\"value", r"back\\slash=value"))
def test_project_env_quoted_codec_round_trips_special_values(tmp_path, value):
    write_project_env_assignments(tmp_path, {"PICO_TEST_SECRET": value})

    assert read_project_env(tmp_path, warn=False)["PICO_TEST_SECRET"] == value


def test_project_env_replace_failure_preserves_original(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"PICO_PROVIDER=deepseek\n")
    monkeypatch.setattr(
        Path,
        "replace",
        lambda self, target: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        write_project_env_assignments(tmp_path, {"PICO_PROVIDER": "anthropic"})

    assert env_path.read_bytes() == b"PICO_PROVIDER=deepseek\n"


def test_project_env_rejects_leaf_symlink(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-env"
    outside.write_text("PICO_PROVIDER=deepseek\n", encoding="utf-8")
    (tmp_path / ".env").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        read_project_env(tmp_path)


def test_init_project_env_error_is_stable_and_omits_sensitive_path(tmp_path, capsys):
    marker = "sk-sensitive-config-path-123456789"
    outside = tmp_path.parent / marker
    outside.write_text("PICO_PROVIDER=deepseek\n", encoding="utf-8")
    (tmp_path / ".env").symlink_to(outside)

    code = main(["--cwd", str(tmp_path), "init", "--provider", "deepseek"])

    captured = capsys.readouterr()
    assert code == 3
    assert marker not in captured.out + captured.err
    assert "project environment read failed" in captured.out + captured.err


def test_project_env_existing_file_is_private_before_read(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX mode assertion")
    env_path = tmp_path / ".env"
    env_path.write_text("PICO_PROVIDER=deepseek\n", encoding="utf-8")
    env_path.chmod(0o644)

    assert read_project_env(tmp_path, warn=False)["PICO_PROVIDER"] == "deepseek"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_project_env_chmod_failure_fails_before_returning_values(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("PICO_API_KEY=opaque-value\n", encoding="utf-8")
    real_chmod = Path.chmod

    def fail_env_chmod(self, *args, **kwargs):
        if self == env_path:
            raise PermissionError("chmod denied")
        return real_chmod(self, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", fail_env_chmod)

    with pytest.raises(PermissionError, match="chmod denied"):
        read_project_env(tmp_path, warn=False)


def test_project_env_rejects_symlinked_private_parent_and_lock(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-config"
    outside.mkdir()
    (tmp_path / ".pico").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        write_project_env_assignments(tmp_path, {"PICO_PROVIDER": "deepseek"})
    assert list(outside.iterdir()) == []

    (tmp_path / ".pico").unlink()
    (tmp_path / ".pico").mkdir(mode=0o700)
    lock_target = outside / "lock-target"
    lock_target.write_text("untouched", encoding="utf-8")
    (tmp_path / ".pico" / "project-env.lock").symlink_to(lock_target)

    with pytest.raises(ValueError, match="symlink"):
        write_project_env_assignments(tmp_path, {"PICO_PROVIDER": "deepseek"})
    assert lock_target.read_text(encoding="utf-8") == "untouched"


def test_project_env_temp_fsync_failure_preserves_original(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    original = b"PICO_PROVIDER=deepseek\n"
    env_path.write_bytes(original)
    real_fsync = os.fsync
    calls = {"count": 0}

    def fail_first_fsync(fd):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("temp fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_fsync)

    with pytest.raises(OSError, match="temp fsync failed"):
        write_project_env_assignments(tmp_path, {"PICO_PROVIDER": "anthropic"})
    assert env_path.read_bytes() == original


@pytest.mark.parametrize(
    "url",
    (
        "https://user:opaque-password@example.test/v1",
        "https://example.test/v1?api_key=opaque-value",
        "https://example.test/v1?token=opaque-value",
    ),
)
def test_credential_bearing_base_url_is_rejected_at_config_boundary(tmp_path, url, capsys):
    code = main([
        "--cwd",
        str(tmp_path),
        "init",
        "--provider",
        "deepseek",
        "--base-url",
        url,
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert "opaque" not in captured.out + captured.err
    assert not (tmp_path / ".env").exists()
