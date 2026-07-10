"""Trusted executable discovery and hardened internal subprocess runners."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

from pico.security import require_regular_no_symlink

AUTO_TRUSTED_EXECUTABLES = ("git", "pwd", "ls", "stat", "file", "wc")
INTERNAL_TRUSTED_EXECUTABLES = ("rg",)
APPROVAL_TRUSTED_EXECUTABLES = (
    "python",
    "python3",
    "uv",
    "pytest",
    "ruff",
    "mypy",
    "pyright",
    "npm",
    "pnpm",
    "yarn",
    "cargo",
    "go",
    "sudo",
    "doas",
    "pkexec",
    "sh",
    "bash",
    "zsh",
    "node",
    "ruby",
    "perl",
    "php",
)
DEFAULT_TRUSTED_EXECUTABLES = (
    *AUTO_TRUSTED_EXECUTABLES,
    *INTERNAL_TRUSTED_EXECUTABLES,
    *APPROVAL_TRUSTED_EXECUTABLES,
)
_GIT_CONFIG_OVERRIDES = (
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    "diff.external=",
    "credential.helper=",
    "protocol.ext.allow=never",
    "pager.status=false",
)
_ENV_ALLOWLIST = ("HOME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")


def discover_lexical_repo_root(cwd):
    current = Path(cwd).resolve()
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        if marker.is_symlink():
            raise ValueError("unsafe .git symlink")
        if marker.is_dir() or marker.is_file():
            return candidate
    return current


def _safe_path_dirs(workspace_root, env):
    root = Path(workspace_root).resolve()
    result = []
    for raw in str((env or os.environ).get("PATH", "")).split(os.pathsep):
        candidate = Path(raw)
        if not raw or raw == "." or not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve(strict=True)
            mode = resolved.stat().st_mode
        except (OSError, RuntimeError):
            continue
        if resolved == root or root in resolved.parents:
            continue
        if not stat.S_ISDIR(mode) or mode & (stat.S_IWGRP | stat.S_IWOTH):
            continue
        value = str(resolved)
        if value not in result:
            result.append(value)
    return result


def build_trusted_executables(workspace_root, *, env=None, names=()):
    root = Path(workspace_root).resolve()
    safe_path_dirs = _safe_path_dirs(root, env)
    if not safe_path_dirs:
        return {}
    search_path = os.pathsep.join(safe_path_dirs)
    result = {}
    for raw_name in tuple(names or DEFAULT_TRUSTED_EXECUTABLES):
        name = str(raw_name)
        if not name or Path(name).name != name:
            continue
        found = shutil.which(name, path=search_path)
        if not found:
            continue
        try:
            resolved = Path(found).resolve(strict=True)
            if resolved == root or root in resolved.parents:
                continue
            require_regular_no_symlink(resolved)
            mode = resolved.stat().st_mode
        except (OSError, RuntimeError, ValueError):
            continue
        if mode & (stat.S_IWGRP | stat.S_IWOTH) or not os.access(resolved, os.X_OK):
            continue
        result[name] = str(resolved)
    return result


def _absolute_executable(executable):
    path = Path(executable)
    if not path.is_absolute():
        raise ValueError("trusted executable must be absolute")
    return str(path)


def _minimal_env(cwd, executable):
    source = os.environ
    env = {name: source[name] for name in _ENV_ALLOWLIST if source.get(name)}
    path_value = os.pathsep.join(
        _safe_path_dirs(cwd, {"PATH": os.pathsep.join((str(Path(executable).parent), source.get("PATH", "")))})
    )
    env["PATH"] = path_value
    return env


def run_hardened_git(executable, args, *, cwd, timeout=5, check=False, text=False):
    executable = _absolute_executable(executable)
    argv = [executable, "--no-pager"]
    for override in _GIT_CONFIG_OVERRIDES:
        argv.extend(("-c", override))
    argv.extend(str(arg) for arg in args)
    env = _minimal_env(cwd, executable)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(
        argv,
        cwd=Path(cwd).resolve(),
        capture_output=True,
        text=text,
        check=check,
        timeout=timeout,
        env=env,
        shell=False,
    )


def run_hardened_rg(executable, args, *, cwd, timeout=20):
    executable = _absolute_executable(executable)
    argv_args = [str(arg) for arg in args]
    if any(
        arg == "--pre"
        or arg.startswith("--pre=")
        or arg == "--pre-glob"
        or arg.startswith("--pre-glob=")
        for arg in argv_args
    ):
        raise ValueError("unsafe ripgrep preprocessing option")
    env = _minimal_env(cwd, executable)
    env["RIPGREP_CONFIG_PATH"] = os.devnull
    return subprocess.run(
        [executable, *argv_args],
        cwd=Path(cwd).resolve(),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
        shell=False,
    )
