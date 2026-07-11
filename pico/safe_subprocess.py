"""Trusted executable discovery and hardened internal subprocess runners."""

import os
import re
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
    "core.askPass=",
    "diff.external=",
    "credential.helper=",
    "protocol.ext.allow=never",
    "pager.status=false",
)
_GIT_DIFF_RENDERING_SUBCOMMANDS = {
    "annotate",
    "blame",
    "diff",
    "log",
    "range-diff",
    "show",
    "whatchanged",
}
_GIT_SAFE_REV_PARSE_ARGS = {
    ("--show-toplevel",),
    ("--is-inside-work-tree",),
}
_GIT_INDEX_RECORD_RE = re.compile(
    rb"(?P<mode>[0-7]{6}) (?:[0-9a-f]{40}|[0-9a-f]{64}) [0-3]\t.+",
    re.DOTALL,
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


def _lexical_git_repository_kind(cwd):
    current = Path(cwd).resolve()
    lexical_root = discover_lexical_repo_root(current)
    marker = lexical_root / ".git"
    if marker.is_dir():
        return "directory"
    if marker.is_file():
        # TODO(Task12): choose verified linked/submodule layouts or explicit trust;
        # separate-git-dir and arbitrary gitdir pointers are lexically indistinguishable.
        return "gitfile"
    for candidate in (current, *current.parents):
        try:
            head_mode = (candidate / "HEAD").lstat().st_mode
            config_mode = (candidate / "config").lstat().st_mode
            objects_mode = (candidate / "objects").lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError("unsafe git repository") from None
        if (
            stat.S_ISREG(head_mode)
            and stat.S_ISREG(config_mode)
            and stat.S_ISDIR(objects_mode)
        ):
            return "bare"
        raise ValueError("unsafe git repository")
    return ""


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


def _frozen_executable_env(executable):
    source = os.environ
    env = {name: source[name] for name in _ENV_ALLOWLIST if source.get(name)}
    env["PATH"] = str(Path(executable).parent)
    return env


def _validate_hardened_git_args(args):
    argv = tuple(str(arg) for arg in args)
    if not argv:
        return argv, ""
    subcommand = argv[0]
    if subcommand.startswith("-") or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9-]*",
        subcommand,
    ):
        raise ValueError("unsafe git arguments")
    if subcommand.casefold() == "submodule" or any(
        arg in {"--ext-diff", "--textconv"}
        or arg.startswith("--ext-diff=")
        or arg.startswith("--textconv=")
        for arg in argv[1:]
    ):
        raise ValueError("unsafe git arguments")
    return argv, subcommand


def _hardened_git_prefix(executable, *, skip_config_keys=()):
    skipped = {key.casefold() for key in skip_config_keys}
    argv = [executable, "--no-pager", "--no-optional-locks"]
    for override in _GIT_CONFIG_OVERRIDES:
        if override.split("=", 1)[0].casefold() in skipped:
            continue
        argv.extend(("-c", override))
    return argv


def _hardened_git_env(cwd, executable):
    env = _minimal_env(cwd, executable)
    env.update(
        GIT_ALLOW_PROTOCOL="git:http:https:ssh",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def _is_executable_git_config_key(key, *, diff_config_is_neutralized):
    suffix = key.rsplit(".", 1)[-1]
    if key in {
        "credential.helper",
        "core.fsmonitor",
        "core.hookspath",
        "diff.external",
    } or key == "core.pager" or key.startswith("pager."):
        return False
    if key.startswith("filter.") and suffix in {"clean", "smudge", "process"}:
        return True
    if key.startswith("diff.") and suffix in {"command", "textconv"}:
        return not diff_config_is_neutralized
    if (
        key.startswith("credential.")
        and suffix == "helper"
        and key != "credential.helper"
    ):
        return True
    if key.startswith("remote.") and suffix in {
        "proxy",
        "uploadpack",
        "receivepack",
        "vcs",
    }:
        return True
    if key.startswith("merge.") and suffix == "driver":
        return True
    if key.startswith(("browser.", "difftool.", "man.", "mergetool.")) and suffix in {
        "cmd",
        "path",
    }:
        return True
    if key.startswith("sendemail.") and (
        suffix.endswith("cmd") or suffix == "smtpserver"
    ):
        return True
    if key.endswith((".cmd", ".command", ".program")):
        return True
    return key in {
        "core.alternaterefscommand",
        "core.askpass",
        "core.editor",
        "core.gitproxy",
        "core.sshcommand",
        "gc.recentobjectshook",
        "gpg.ssh.defaultkeycommand",
        "instaweb.httpd",
        "interactive.difffilter",
        "sequence.editor",
        "uploadpack.packobjectshook",
    }


def _validate_gitfile_worktree_root(executable, *, cwd, timeout):
    lexical_root = discover_lexical_repo_root(cwd)
    argv = _hardened_git_prefix(executable)
    argv.extend(("-c", "alias.rev-parse="))
    argv.extend(("rev-parse", "--show-toplevel"))
    result = subprocess.run(
        argv,
        cwd=Path(cwd).resolve(),
        capture_output=True,
        text=False,
        check=False,
        timeout=timeout,
        env=_hardened_git_env(cwd, executable),
        shell=False,
    )
    if result.returncode != 0 or not isinstance(result.stdout, (bytes, bytearray)):
        raise ValueError("unsafe git repository config")
    try:
        reported_root = bytes(result.stdout).decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ValueError("unsafe git repository config") from None
    if not reported_root or Path(reported_root).resolve() != lexical_root:
        raise ValueError("unsafe git repository config")


def _validate_hardened_git_repository(
    executable,
    *,
    cwd,
    args,
    timeout=5,
):
    args = tuple(str(arg) for arg in args)
    repository_kind = _lexical_git_repository_kind(cwd)
    if not repository_kind:
        return
    safe_exact_query = (
        args[:1] == ("rev-parse",)
        and args[1:] in _GIT_SAFE_REV_PARSE_ARGS
    ) or (
        len(args) == 2
        and args[0] == "show"
        and args[1].startswith("HEAD:")
        and len(args[1]) > len("HEAD:")
    )
    executable = _absolute_executable(executable)
    probe_timeout = min(int(timeout), 5)
    argv = _hardened_git_prefix(
        executable,
        skip_config_keys={"core.askpass"},
    )
    argv.extend(("-c", "alias.config="))
    argv.extend(
        (
            "config",
            "--includes",
            "--null",
            "--name-only",
            "--list",
        )
    )
    result = subprocess.run(
        argv,
        cwd=Path(cwd).resolve(),
        capture_output=True,
        text=False,
        check=False,
        timeout=probe_timeout,
        env=_hardened_git_env(cwd, executable),
        shell=False,
    )
    if result.returncode != 0 or not isinstance(
        result.stdout,
        (bytes, bytearray),
    ):
        raise ValueError("unsafe git repository config")
    try:
        keys = [
            raw.decode("utf-8").casefold()
            for raw in bytes(result.stdout).split(b"\x00")
            if raw
        ]
    except UnicodeDecodeError:
        raise ValueError("unsafe git repository config") from None
    if "core.worktree" in keys:
        if repository_kind != "gitfile":
            raise ValueError("unsafe git repository config")
        _validate_gitfile_worktree_root(
            executable,
            cwd=cwd,
            timeout=probe_timeout,
        )
    if safe_exact_query:
        return
    diff_config_is_neutralized = args[:1] in {
        ("annotate",),
        ("blame",),
        ("diff",),
        ("log",),
        ("show",),
        ("whatchanged",),
    } or args[:2] == ("reflog", "show")
    if any(
        _is_executable_git_config_key(
            key,
            diff_config_is_neutralized=diff_config_is_neutralized,
        )
        for key in keys
    ):
        raise ValueError("unsafe git repository config")

    argv = _hardened_git_prefix(executable)
    argv.extend(("-c", "alias.ls-files="))
    argv.extend(("ls-files", "--stage", "-z"))
    result = subprocess.run(
        argv,
        cwd=Path(cwd).resolve(),
        capture_output=True,
        text=False,
        check=False,
        timeout=probe_timeout,
        env=_hardened_git_env(cwd, executable),
        shell=False,
    )
    if result.returncode != 0 or not isinstance(
        result.stdout,
        (bytes, bytearray),
    ):
        raise ValueError("unsafe git repository config")
    records = bytes(result.stdout).split(b"\x00")
    if not records or records[-1] != b"":
        raise ValueError("unsafe git repository config")
    for entry in records[:-1]:
        match = _GIT_INDEX_RECORD_RE.fullmatch(entry)
        if match is None or match.group("mode") == b"160000":
            raise ValueError("unsafe git repository config")


def run_hardened_git(executable, args, *, cwd, timeout=5, check=False, text=False):
    executable = _absolute_executable(executable)
    args, subcommand = _validate_hardened_git_args(args)
    _validate_hardened_git_repository(
        executable,
        cwd=cwd,
        args=args,
        timeout=timeout,
    )
    argv = _hardened_git_prefix(executable)
    if subcommand:
        argv.extend(("-c", f"alias.{subcommand}="))
    hardened_args = list(args)
    if subcommand in _GIT_DIFF_RENDERING_SUBCOMMANDS:
        hardened_args[1:1] = ["--no-ext-diff", "--no-textconv"]
    elif subcommand == "reflog" and len(hardened_args) > 1:
        if hardened_args[1] == "show":
            hardened_args[2:2] = ["--no-ext-diff", "--no-textconv"]
        elif hardened_args[1].startswith("-"):
            hardened_args[1:1] = [
                "show",
                "--no-ext-diff",
                "--no-textconv",
            ]
    argv.extend(hardened_args)
    return subprocess.run(
        argv,
        cwd=Path(cwd).resolve(),
        capture_output=True,
        text=text,
        check=check,
        timeout=timeout,
        env=_hardened_git_env(cwd, executable),
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
    env = _frozen_executable_env(executable)
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
