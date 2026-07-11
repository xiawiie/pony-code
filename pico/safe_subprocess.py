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
_GIT_CONFIG_SECTION_RE = re.compile(
    r'^\[\s*(?P<name>[A-Za-z0-9][A-Za-z0-9.-]*)'
    r'(?:\s+"(?:[^"\\]|\\.)*")?\s*\]\s*(?:[#;].*)?$'
)
_GIT_CONFIG_KEY_RE = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(?P<value>.*))?$"
)
_MAX_GIT_METADATA_BYTES = 64 * 1024
# Regular gitfiles fail closed without race-safe component traversal.
_HAS_GIT_DIR_FD_TRAVERSAL = (
    os.name == "posix"
    and bool(getattr(os, "O_DIRECTORY", 0))
    and bool(getattr(os, "O_NOFOLLOW", 0))
    and os.open in getattr(os, "supports_dir_fd", ())
    and os.stat in getattr(os, "supports_dir_fd", ())
)
_ENV_ALLOWLIST = ("HOME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")


def _open_lexical_repo_root(cwd):
    current = Path(cwd).resolve()
    for candidate in (current, *current.parents):
        _, candidate_fd = _open_git_path(candidate, directory=True)
        try:
            mode = os.stat(
                ".git",
                dir_fd=candidate_fd,
                follow_symlinks=False,
            ).st_mode
        except FileNotFoundError:
            os.close(candidate_fd)
            continue
        except Exception:
            os.close(candidate_fd)
            raise
        if stat.S_ISLNK(mode):
            os.close(candidate_fd)
            raise ValueError("unsafe .git symlink")
        if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
            return candidate, candidate_fd, mode
        os.close(candidate_fd)
        raise ValueError("unsafe git repository")
    _, current_fd = _open_git_path(current, directory=True)
    return current, current_fd, None


def discover_lexical_repo_root(cwd):
    if _HAS_GIT_DIR_FD_TRAVERSAL:
        root, root_fd, _ = _open_lexical_repo_root(cwd)
        os.close(root_fd)
        return root

    current = Path(cwd).resolve()
    for candidate in (current, *current.parents):
        marker = candidate / ".git"
        try:
            mode = marker.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError("unsafe git repository") from None
        if stat.S_ISLNK(mode):
            raise ValueError("unsafe .git symlink")
        if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
            return candidate
        raise ValueError("unsafe git repository")
    return current


def _metadata_candidate(base, value):
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return Path(os.path.abspath(candidate))


def _git_open_flags(*, directory):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    return flags


def _open_git_path(path, *, directory):
    if not _HAS_GIT_DIR_FD_TRAVERSAL:
        raise ValueError("unsafe git repository")
    candidate = Path(os.path.abspath(path))
    descriptor = os.open(candidate.anchor, _git_open_flags(directory=True))
    try:
        components = candidate.parts[1:]
        for index, component in enumerate(components):
            final = index == len(components) - 1
            next_descriptor = os.open(
                component,
                _git_open_flags(directory=not final or directory),
                dir_fd=descriptor,
            )
            try:
                opened = os.fstat(next_descriptor)
                expected_type = stat.S_ISDIR if not final or directory else stat.S_ISREG
                if not expected_type(opened.st_mode):
                    raise ValueError("unsafe git repository")
            except Exception:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        opened = os.fstat(descriptor)
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_type(opened.st_mode):
            raise ValueError("unsafe git repository")
        return candidate, descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_git_entry(directory_fd, name, *, directory):
    raw_name = os.fsdecode(name)
    if Path(raw_name).name != raw_name or raw_name in {"", ".", ".."}:
        raise ValueError("unsafe git repository")
    if not _HAS_GIT_DIR_FD_TRAVERSAL:
        raise ValueError("unsafe git repository")
    return os.open(
        raw_name,
        _git_open_flags(directory=directory),
        dir_fd=directory_fd,
    )


def _read_git_metadata(path, *, dir_fd=None, allow_missing=False):
    descriptor = -1
    try:
        if dir_fd is None:
            _, descriptor = _open_git_path(path, directory=False)
        else:
            descriptor = _open_git_entry(dir_fd, path, directory=False)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > _MAX_GIT_METADATA_BYTES
        ):
            raise ValueError("unsafe git repository")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(_MAX_GIT_METADATA_BYTES + 1)
        if len(data) > _MAX_GIT_METADATA_BYTES:
            raise ValueError("unsafe git repository")
        return data
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _single_git_path(data, *, prefix=""):
    if data.endswith(b"\n"):
        data = data[:-1]
    if not data or b"\n" in data or b"\r" in data or b"\x00" in data:
        raise ValueError("unsafe git repository")
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("unsafe git repository") from None
    if prefix:
        if not value.startswith(prefix):
            raise ValueError("unsafe git repository")
        value = value[len(prefix) :]
    if not value or value != value.strip():
        raise ValueError("unsafe git repository")
    return value


def _open_metadata_directory(base, value):
    candidate = _metadata_candidate(base, value)
    return _open_git_path(candidate, directory=True)


def _metadata_directory(base, value):
    candidate, descriptor = _open_metadata_directory(base, value)
    os.close(descriptor)
    return candidate


def _metadata_file(base, value):
    candidate = _metadata_candidate(base, value)
    _read_git_metadata(candidate)
    return candidate


def _git_config_value(raw_value):
    value = (raw_value or "").strip()
    if not value:
        raise ValueError("unsafe git repository")
    if not value.startswith('"'):
        for comment in ("#", ";"):
            value = value.split(comment, 1)[0]
        value = value.rstrip()
        if not value or '"' in value or "\\" in value:
            raise ValueError("unsafe git repository")
        return value

    result = []
    escaped = False
    for index, char in enumerate(value[1:], start=1):
        if escaped:
            try:
                result.append(
                    {"b": "\b", "n": "\n", "t": "\t", "\\": "\\", '"': '"'}[
                        char
                    ]
                )
            except KeyError:
                raise ValueError("unsafe git repository") from None
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            tail = value[index + 1 :].strip()
            if tail and not tail.startswith(("#", ";")):
                raise ValueError("unsafe git repository")
            parsed = "".join(result)
            if not parsed or any(ord(item) < 32 for item in parsed):
                raise ValueError("unsafe git repository")
            return parsed
        else:
            result.append(char)
    raise ValueError("unsafe git repository")


def _absorbed_submodule_worktree(config_data):
    try:
        text = config_data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("unsafe git repository") from None
    if "\x00" in text:
        raise ValueError("unsafe git repository")

    section = ""
    worktrees = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("["):
            match = _GIT_CONFIG_SECTION_RE.fullmatch(stripped)
            if match is None:
                raise ValueError("unsafe git repository")
            section = match.group("name").casefold()
            if section in {"alias", "include", "includeif"}:
                raise ValueError("unsafe git repository")
            continue
        match = _GIT_CONFIG_KEY_RE.fullmatch(stripped)
        if match is None or not section:
            raise ValueError("unsafe git repository")
        key = f"{section}.{match.group('name').casefold()}"
        if key in {
            "core.askpass",
            "core.fsmonitor",
            "core.hookspath",
            "credential.helper",
            "diff.external",
            "extensions.worktreeconfig",
        } or _is_executable_git_config_key(
            key,
            diff_config_is_neutralized=False,
        ):
            raise ValueError("unsafe git repository")
        if key == "core.worktree":
            worktrees.append(_git_config_value(match.group("value")))
    if len(worktrees) != 1:
        raise ValueError("unsafe git repository")
    return worktrees[0]


def _validate_linked_worktree_gitfile(marker, target, target_fd, backlink_data):
    backlink = _metadata_file(
        target,
        _single_git_path(backlink_data),
    )
    if backlink != marker:
        raise ValueError("unsafe git repository")
    common, common_fd = _open_metadata_directory(
        target,
        _single_git_path(_read_git_metadata("commondir", dir_fd=target_fd)),
    )
    try:
        if target.parent.name != "worktrees" or target.parent.parent != common:
            raise ValueError("unsafe git repository")
        _read_git_metadata("HEAD", dir_fd=target_fd)
        _read_git_metadata("HEAD", dir_fd=common_fd)
        _read_git_metadata("config", dir_fd=common_fd)
        _read_git_metadata(
            "config.worktree",
            dir_fd=target_fd,
            allow_missing=True,
        )
    finally:
        os.close(common_fd)
    return common


def _open_gitfile_target(marker, marker_data=None):
    return _open_metadata_directory(
        marker.parent,
        _single_git_path(
            _read_git_metadata(marker) if marker_data is None else marker_data,
            prefix="gitdir: ",
        ),
    )


def _enclosing_git_dir(lexical_root):
    for super_root in lexical_root.parents:
        marker = super_root / ".git"
        _, super_fd = _open_git_path(super_root, directory=True)
        marker_data = None
        try:
            try:
                mode = os.stat(
                    ".git",
                    dir_fd=super_fd,
                    follow_symlinks=False,
                ).st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise ValueError("unsafe git repository")
            if stat.S_ISDIR(mode):
                common = _metadata_candidate(super_root, ".git")
                common_fd = _open_git_entry(super_fd, ".git", directory=True)
                try:
                    _read_git_metadata("HEAD", dir_fd=common_fd)
                    _read_git_metadata("config", dir_fd=common_fd)
                finally:
                    os.close(common_fd)
                return common
            if not stat.S_ISREG(mode):
                raise ValueError("unsafe git repository")
            marker_data = _read_git_metadata(".git", dir_fd=super_fd)
        finally:
            os.close(super_fd)
        _, git_dir = _validate_gitfile_binding(
            super_root,
            marker,
            marker_data=marker_data,
        )
        return git_dir
    raise ValueError("unsafe git repository")


def _validate_absorbed_submodule_gitfile(lexical_root, target, target_fd):
    worktree = _absorbed_submodule_worktree(
        _read_git_metadata("config", dir_fd=target_fd)
    )
    if _metadata_directory(target, worktree) != lexical_root:
        raise ValueError("unsafe git repository")
    super_git_dir = _enclosing_git_dir(lexical_root)
    modules, modules_fd = _open_metadata_directory(super_git_dir, "modules")
    os.close(modules_fd)
    try:
        relative_target = target.relative_to(modules)
    except ValueError:
        raise ValueError("unsafe git repository") from None
    if not relative_target.parts:
        raise ValueError("unsafe git repository")
    _read_git_metadata("HEAD", dir_fd=target_fd)


def _validate_gitfile_binding(lexical_root, marker, *, marker_data=None):
    try:
        target, target_fd = _open_gitfile_target(marker, marker_data)
        try:
            backlink_data = _read_git_metadata(
                "gitdir",
                dir_fd=target_fd,
                allow_missing=True,
            )
            if backlink_data is None:
                _validate_absorbed_submodule_gitfile(
                    lexical_root,
                    target,
                    target_fd,
                )
                return "absorbed-submodule", target
            _validate_linked_worktree_gitfile(
                marker,
                target,
                target_fd,
                backlink_data,
            )
            return "linked-worktree", target
        finally:
            os.close(target_fd)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("unsafe git repository config") from None


def _lexical_git_repository_kind(cwd):
    current = Path(cwd).resolve()
    if _HAS_GIT_DIR_FD_TRAVERSAL:
        lexical_root, root_fd, marker_mode = _open_lexical_repo_root(current)
        marker = lexical_root / ".git"
        marker_data = None
        try:
            if marker_mode is not None:
                if stat.S_ISDIR(marker_mode):
                    return "directory"
                marker_data = _read_git_metadata(".git", dir_fd=root_fd)
        finally:
            os.close(root_fd)
        if marker_data is not None:
            return _validate_gitfile_binding(
                lexical_root,
                marker,
                marker_data=marker_data,
            )[0]
    else:
        lexical_root = discover_lexical_repo_root(current)
        marker = lexical_root / ".git"
        if marker.is_dir():
            return "directory"
        if marker.is_file():
            return _validate_gitfile_binding(lexical_root, marker)[0]
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
        if repository_kind != "absorbed-submodule":
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
