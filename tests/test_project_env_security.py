import os
import stat
from contextlib import contextmanager

import pytest

import pony.config.environment as config_module
from pony.security import private_files as security_module
from pony.cli.app import main
from pony.config.environment import (
    project_env_path,
    read_project_env,
    read_project_env_with_status,
    write_project_env_assignments,
)


def test_project_env_status_distinguishes_missing_loaded_and_rejected_lines(
    tmp_path,
    capsys,
):
    values, metadata = read_project_env_with_status(tmp_path)
    assert values == {}
    assert metadata == {
        "path": str(tmp_path.resolve() / ".env"),
        "scope": "repo_root_exact",
        "status": "missing",
    }

    (tmp_path / ".env").write_text(
        "PONY_TEST_SETTING=deepseek\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").chmod(0o600)
    values, metadata = read_project_env_with_status(tmp_path)
    assert values == {"PONY_TEST_SETTING": "deepseek"}
    assert metadata["status"] == "loaded"

    (tmp_path / ".env").write_text(
        "PONY_TEST_SETTING=deepseek\ninvalid project env line\n",
        encoding="utf-8",
    )
    values, metadata = read_project_env_with_status(tmp_path)
    captured = capsys.readouterr()
    assert values == {"PONY_TEST_SETTING": "deepseek"}
    assert metadata["status"] == "review_required"
    assert "invalid project env line" not in captured.err


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode assertion")
def test_project_env_status_records_permission_repair(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("PONY_TEST_SETTING=deepseek\n", encoding="utf-8")
    env_path.chmod(0o644)

    values, metadata = read_project_env_with_status(tmp_path, warn=False)

    assert values == {"PONY_TEST_SETTING": "deepseek"}
    assert metadata["status"] == "review_required"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_project_env_never_falls_back_to_parent(tmp_path):
    parent = tmp_path / ".env"
    child = tmp_path / "repo"
    child.mkdir()
    parent.write_text("PONY_TEST_SETTING=anthropic\n", encoding="utf-8")

    assert project_env_path(child) == child.resolve() / ".env"
    assert read_project_env(child, warn=False) == {}


def test_project_env_read_and_write_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "MAX_PROJECT_ENV_BYTES", 32)
    env_path = tmp_path / ".env"
    oversized = b"PONY_VALUE=" + b"x" * 32 + b"\n"
    env_path.write_bytes(oversized)

    with pytest.raises(ValueError, match="private file too large"):
        read_project_env(tmp_path, warn=False)
    with pytest.raises(ValueError, match="private file too large"):
        write_project_env_assignments(tmp_path, {"PONY_VALUE": "small"})

    assert env_path.read_bytes() == oversized
    assert not list(tmp_path.glob(".*.bak"))


def test_project_env_rejects_oversized_new_content(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "MAX_PROJECT_ENV_BYTES", 32)

    with pytest.raises(ValueError, match="private file too large"):
        write_project_env_assignments(tmp_path, {"PONY_VALUE": "x" * 64})

    assert not (tmp_path / ".env").exists()


def test_read_project_env_never_mutates_process_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "PONY_SECRET_ENV_NAMES=PATH,PYTHONPATH\n"
        "PATH=./fake\n"
        "PYTHONPATH=./payload\n"
        "PONY_TEST_SETTING=deepseek\n",
        encoding="utf-8",
    )
    original_path = os.environ.get("PATH")
    monkeypatch.delenv("PYTHONPATH", raising=False)

    loaded = read_project_env(tmp_path)

    assert loaded["PONY_TEST_SETTING"] == "deepseek"
    assert loaded["PATH"] == "./fake"
    assert loaded["PYTHONPATH"] == "./payload"
    assert os.environ.get("PATH") == original_path
    assert "PYTHONPATH" not in os.environ


@pytest.mark.parametrize("value", (" a # b ", "quote'\"value", r"back\\slash=value"))
def test_project_env_quoted_codec_round_trips_special_values(tmp_path, value):
    write_project_env_assignments(tmp_path, {"PONY_TEST_SECRET": value})

    assert read_project_env(tmp_path, warn=False)["PONY_TEST_SECRET"] == value


def test_project_env_rejects_control_characters_after_decoding(
    tmp_path, monkeypatch, capsys
):
    sentinel = "opaque-control-value-123456789"
    invalid_names = (
        "PONY_JSON_NUL",
        "PONY_JSON_NEWLINE",
        "PONY_JSON_CARRIAGE_RETURN",
        "PONY_UNQUOTED_NUL",
    )
    (tmp_path / ".env").write_text(
        f'PONY_JSON_NUL="{sentinel}\\u0000tail"\n'
        f'PONY_JSON_NEWLINE="{sentinel}\\ntail"\n'
        f'PONY_JSON_CARRIAGE_RETURN="{sentinel}\\rtail"\n'
        f"PONY_UNQUOTED_NUL={sentinel}\0tail\n"
        "PONY_TEST_SETTING=deepseek\n",
        encoding="utf-8",
    )
    for name in invalid_names:
        monkeypatch.delenv(name, raising=False)

    parsed = read_project_env(tmp_path)

    assert parsed == {"PONY_TEST_SETTING": "deepseek"}
    assert all(name not in os.environ for name in invalid_names)
    assert sentinel not in capsys.readouterr().err


def test_project_env_replace_failure_preserves_original(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"PONY_TEST_SETTING=deepseek\n")
    monkeypatch.setattr(
        security_module.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        write_project_env_assignments(tmp_path, {"PONY_TEST_SETTING": "anthropic"})

    assert env_path.read_bytes() == b"PONY_TEST_SETTING=deepseek\n"


def test_project_env_rejects_leaf_symlink(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-env"
    outside.write_text("PONY_TEST_SETTING=deepseek\n", encoding="utf-8")
    (tmp_path / ".env").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        read_project_env(tmp_path)


def test_init_project_env_error_is_stable_and_omits_sensitive_path(tmp_path, capsys):
    marker = "sk-sensitive-config-path-123456789"
    outside = tmp_path.parent / marker
    outside.write_text("PONY_TEST_SETTING=deepseek\n", encoding="utf-8")
    (tmp_path / ".env").symlink_to(outside)

    code = main(
        [
        "--cwd",
        str(tmp_path),
        "init",
        ]
    )

    captured = capsys.readouterr()
    assert code == 3
    assert marker not in captured.out + captured.err
    assert "project environment read failed" in captured.out + captured.err


def test_project_env_existing_file_is_private_before_read(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX mode assertion")
    env_path = tmp_path / ".env"
    env_path.write_text("PONY_TEST_SETTING=deepseek\n", encoding="utf-8")
    env_path.chmod(0o644)

    assert read_project_env(tmp_path, warn=False)["PONY_TEST_SETTING"] == "deepseek"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_project_env_non_hardening_read_still_rejects_non_private_file(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("PONY_TEST_SETTING=deepseek\n", encoding="utf-8")
    env_path.chmod(0o644)

    with pytest.raises(ValueError, match="private file permissions are unsafe"):
        read_project_env(tmp_path, warn=False, harden=False)

    assert stat.S_IMODE(env_path.stat().st_mode) == 0o644


def test_project_env_chmod_failure_fails_before_returning_values(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("PONY_API_KEY=opaque-value\n", encoding="utf-8")

    def fail_env_chmod(_descriptor, _mode):
        raise PermissionError("chmod denied")

    monkeypatch.setattr(security_module.os, "fchmod", fail_env_chmod)

    with pytest.raises(PermissionError, match="chmod denied"):
        read_project_env(tmp_path, warn=False)


def test_project_env_reads_verified_descriptor_after_leaf_swap(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("PONY_TEST_SETTING=deepseek\n", encoding="utf-8")
    outside = tmp_path / "outside.env"
    outside.write_text("PONY_TEST_SETTING=anthropic\n", encoding="utf-8")
    real_fchmod = security_module.os.fchmod
    swapped = False

    def swap_after_validation(descriptor, mode):
        nonlocal swapped
        real_fchmod(descriptor, mode)
        if not swapped:
            env_path.unlink()
            env_path.symlink_to(outside)
            swapped = True

    monkeypatch.setattr(security_module.os, "fchmod", swap_after_validation)

    assert read_project_env(tmp_path, warn=False) == {"PONY_TEST_SETTING": "deepseek"}


def test_project_env_rejects_symlinked_private_parent_and_lock(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-config"
    outside.mkdir()
    (tmp_path / ".pony").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        write_project_env_assignments(tmp_path, {"PONY_TEST_SETTING": "deepseek"})
    assert list(outside.iterdir()) == []

    (tmp_path / ".pony").unlink()
    (tmp_path / ".pony").mkdir(mode=0o700)
    lock_target = outside / "lock-target"
    lock_target.write_text("untouched", encoding="utf-8")
    (tmp_path / ".pony" / "project-env.lock").symlink_to(lock_target)

    with pytest.raises(ValueError, match="symlink"):
        write_project_env_assignments(tmp_path, {"PONY_TEST_SETTING": "deepseek"})
    assert lock_target.read_text(encoding="utf-8") == "untouched"


def test_project_env_temp_fsync_failure_preserves_original(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    original = b"PONY_TEST_SETTING=deepseek\n"
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
        write_project_env_assignments(tmp_path, {"PONY_TEST_SETTING": "anthropic"})
    assert env_path.read_bytes() == original


def test_project_env_rejects_swapped_temp_inode_before_replace(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    original = b"PONY_TEST_SETTING=deepseek\n"
    env_path.write_bytes(original)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-swap"
    outside_bytes = b"PONY_TEST_SETTING=outside\n"
    outside.write_bytes(outside_bytes)
    real_fsync = os.fsync
    swapped = {}

    def swap_temp_after_fsync(fd):
        real_fsync(fd)
        if swapped:
            return
        temp_path = next(tmp_path.glob(".*.tmp"))
        temp_path.unlink()
        os.link(outside, temp_path)
        swapped["path"] = temp_path

    monkeypatch.setattr(os, "fsync", swap_temp_after_fsync)

    with pytest.raises(ValueError, match="project env temp changed"):
        write_project_env_assignments(tmp_path, {"PONY_TEST_SETTING": "anthropic"})

    assert env_path.read_bytes() == original
    assert outside.read_bytes() == outside_bytes
    assert swapped["path"].samefile(outside)


def test_project_env_rejects_temp_hardlink_before_replace(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    original = b"PONY_TEST_SETTING=deepseek\n"
    env_path.write_bytes(original)
    alias = tmp_path.parent / f"{tmp_path.name}-outside-temp-alias"
    real_fsync = os.fsync
    linked = False

    def hardlink_temp_after_fsync(descriptor):
        nonlocal linked
        real_fsync(descriptor)
        if not linked:
            temp_path = next(tmp_path.glob(".*.tmp"))
            os.link(temp_path, alias)
            linked = True

    monkeypatch.setattr(os, "fsync", hardlink_temp_after_fsync)

    with pytest.raises(ValueError, match="project env temp changed"):
        write_project_env_assignments(tmp_path, {"PONY_TEST_SETTING": "anthropic"})

    assert env_path.read_bytes() == original
    assert alias.exists()
    assert alias.read_bytes() == b""
    assert not list(tmp_path.glob(".*.tmp"))


def test_project_env_parent_swap_cannot_redirect_write(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    original = b"PONY_TEST_SETTING=deepseek\n"
    env_path.write_bytes(original)
    original_root = tmp_path.parent / f"{tmp_path.name}-original-root"

    @contextmanager
    def swap_after_root_binding(*_args, **_kwargs):
        tmp_path.rename(original_root)
        tmp_path.mkdir()
        (tmp_path / ".pony").mkdir()
        yield

    monkeypatch.setattr(config_module, "locked_file", swap_after_root_binding)

    with pytest.raises(ValueError, match="private root changed"):
        write_project_env_assignments(tmp_path, {"PONY_TEST_SETTING": "anthropic"})

    assert (original_root / ".env").read_bytes() == original
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize(
    "url",
    (
        "https://user:opaque-password@example.test/v1",
        "https://example.test/v1?api_key=opaque-value",
        "https://example.test/v1?token=opaque-value",
    ),
)
def test_credential_bearing_base_url_is_rejected_at_config_boundary(
    tmp_path,
    url,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr("builtins.input", lambda: url)
    code = main(
        [
        "--cwd",
        str(tmp_path),
        "init",
        ]
    )

    captured = capsys.readouterr()
    assert code == 3
    assert "opaque" not in captured.out + captured.err
    assert not (tmp_path / ".env").exists()
