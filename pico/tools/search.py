"""Bounded, sensitive-path-aware workspace search."""

import os
from pathlib import Path
import stat

from pico.security import paths as security_paths
from pico.security import workspace_files as workspace_files
from pico.tools.subprocess import run_hardened_rg
from pico.workspace.context import IGNORED_PATH_NAMES

from .validation import (
    MAX_WORKSPACE_DIRECTORY_ENTRIES,
    MAX_WORKSPACE_FILE_BYTES,
    MAX_WORKSPACE_SEARCH_BYTES,
    MAX_WORKSPACE_SEARCH_DEPTH,
    MAX_WORKSPACE_SEARCH_FILES,
    MAX_WORKSPACE_SEARCH_MATCHES,
    _anchored_tool_relative,
    _decode_workspace_utf8,
    _workspace_root_identity,
)


_RG_SENSITIVE_GLOBS = (
    "!.env",
    "!.env.*",
    "!.envrc",
    "!.netrc",
    "!.npmrc",
    "!.pypirc",
    "!.git-credentials",
    "!credentials.json",
    "!auth.json",
    "!service-account*.json",
    "!secrets.json",
    "!secrets.yaml",
    "!secrets.yml",
    "!secrets.toml",
    "!*.pem",
    "!*.key",
    "!*.p12",
    "!*.pfx",
    "!*.jks",
    "!*.keystore",
    "!**/.ssh/**",
    "!**/.gnupg/**",
    "!**/.aws/credentials",
    "!**/.docker/config.json",
    "!**/.kube/config",
    "!**/.pico/sessions/**",
    "!**/.pico/runs/**",
    "!**/.pico/checkpoints/**",
)

_ALLOWED_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})


def tool_search(context, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    raw_path = args.get("path", ".")
    relative_path = _anchored_tool_relative(
        context,
        raw_path,
        allow_root=True,
    )
    path = context.path(raw_path)

    rg_executable = context.trusted_executables.get("rg")
    if rg_executable and not _rg_workspace_is_safe(context, relative_path):
        rg_executable = None
    if rg_executable:
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        base_rg_args = [
            "-n",
            "--with-filename",
            "--null",
            "--smart-case",
            "--max-count",
            "200",
            "--max-depth",
            str(MAX_WORKSPACE_SEARCH_DEPTH),
            "--max-filesize",
            str(MAX_WORKSPACE_FILE_BYTES),
        ]
        rg_args = list(base_rg_args)
        try:
            target_is_directory = stat.S_ISDIR(path.lstat().st_mode)
        except OSError:
            target_is_directory = False
        if target_is_directory:
            rg_args.append("--glob-case-insensitive")
            for glob in _RG_SENSITIVE_GLOBS:
                rg_args.extend(("--glob", glob))
        rg_args.extend(("-e", pattern, "--", str(path)))
        result = run_hardened_rg(
            rg_executable,
            rg_args,
            cwd=context.root,
        )
        if result.returncode > 1:
            result.check_returncode()
        filtered = (
            _filter_rg_output(context.root, result.stdout)
            if result.returncode == 0
            else "(no matches)"
        )
        matches = [] if filtered == "(no matches)" else filtered.splitlines()

        if target_is_directory and len(matches) < 200:
            templates = _safe_env_template_files(context, relative_path)
            if templates:
                template_args = [
                    *base_rg_args,
                    "-e",
                    pattern,
                    "--",
                    *(str(template) for template in templates),
                ]
                template_result = run_hardened_rg(
                    rg_executable,
                    template_args,
                    cwd=context.root,
                )
                if template_result.returncode > 1:
                    template_result.check_returncode()
                if template_result.returncode == 0:
                    template_filtered = _filter_rg_output(
                        context.root,
                        template_result.stdout,
                    )
                    if template_filtered != "(no matches)":
                        template_matches = template_filtered.splitlines()
                        matches.extend(template_matches[: 200 - len(matches)])
        return "\n".join(matches) or "(no matches)"

    matches = _python_search_matches(context, relative_path, pattern)
    return "\n".join(matches) or "(no matches)"


def _rg_workspace_is_safe(context, target):
    """Preflight rg's tree so static unsafe entries use the anchored fallback."""
    root = Path(context.root)
    candidate = root if target == "." else root / target
    try:
        target_info = candidate.lstat()
    except OSError:
        return False
    if stat.S_ISREG(target_info.st_mode):
        return (
            target_info.st_nlink == 1
            and target_info.st_size <= MAX_WORKSPACE_FILE_BYTES
        )
    if not stat.S_ISDIR(target_info.st_mode):
        return False

    root_identity = _workspace_root_identity(context)
    stack = [(target, 0)]
    scanned = 0
    files = 0
    while stack:
        directory, depth = stack.pop()
        listing = workspace_files.list_directory_names_anchored(
            root,
            directory,
            max_entries=MAX_WORKSPACE_DIRECTORY_ENTRIES,
            expected_root_identity=root_identity,
        )
        if listing["unsafe_count"]:
            return False
        scanned += listing["scanned"]
        if scanned > MAX_WORKSPACE_SEARCH_FILES:
            raise workspace_files.WorkspaceIOError(
                "workspace_search_limit_exceeded",
                "workspace search entry limit exceeded",
            )
        children = []
        for entry in listing["entries"]:
            relative = (
                entry["name"] if directory == "." else f"{directory}/{entry['name']}"
            )
            if entry["name"] in IGNORED_PATH_NAMES or security_paths.is_sensitive_path(
                relative
            ):
                continue
            if stat.S_ISDIR(entry["mode"]):
                if depth >= MAX_WORKSPACE_SEARCH_DEPTH:
                    raise workspace_files.WorkspaceIOError(
                        "workspace_search_limit_exceeded",
                        "workspace search depth limit exceeded",
                    )
                children.append(relative)
            elif stat.S_ISREG(entry["mode"]):
                files += 1
                if files > MAX_WORKSPACE_SEARCH_FILES:
                    raise workspace_files.WorkspaceIOError(
                        "workspace_search_limit_exceeded",
                        "workspace search file limit exceeded",
                    )
        for child in reversed(children):
            stack.append((child, depth + 1))
    return True


def _python_search_matches(
    context, target, pattern, limit=MAX_WORKSPACE_SEARCH_MATCHES
):
    matches = []
    root = Path(context.root)
    root_identity = _workspace_root_identity(context)
    candidate = root if target == "." else root / target
    try:
        target_mode = candidate.lstat().st_mode
    except OSError:
        raise ValueError("search path does not exist") from None

    files = []
    scanned_entries = 0
    if stat.S_ISREG(target_mode):
        files.append(target)
    elif stat.S_ISDIR(target_mode):
        stack = [(target, 0)]
        while stack:
            directory, depth = stack.pop()
            listing = workspace_files.list_directory_names_anchored(
                root,
                directory,
                max_entries=MAX_WORKSPACE_DIRECTORY_ENTRIES,
                expected_root_identity=root_identity,
            )
            scanned_entries += listing["scanned"]
            if scanned_entries > MAX_WORKSPACE_SEARCH_FILES:
                raise workspace_files.WorkspaceIOError(
                    "workspace_search_limit_exceeded",
                    "workspace search entry limit exceeded",
                )
            child_directories = []
            for entry in listing["entries"]:
                relative = (
                    entry["name"]
                    if directory == "."
                    else f"{directory}/{entry['name']}"
                )
                if entry[
                    "name"
                ] in IGNORED_PATH_NAMES or security_paths.is_sensitive_path(relative):
                    continue
                if stat.S_ISDIR(entry["mode"]):
                    if depth >= MAX_WORKSPACE_SEARCH_DEPTH:
                        raise workspace_files.WorkspaceIOError(
                            "workspace_search_limit_exceeded",
                            "workspace search depth limit exceeded",
                        )
                    child_directories.append(relative)
                elif stat.S_ISREG(entry["mode"]):
                    files.append(relative)
                    if len(files) > MAX_WORKSPACE_SEARCH_FILES:
                        raise workspace_files.WorkspaceIOError(
                            "workspace_search_limit_exceeded",
                            "workspace search file limit exceeded",
                        )
            for child in reversed(child_directories):
                stack.append((child, depth + 1))
    else:
        raise workspace_files.WorkspaceIOError(
            "workspace_entry_unsafe",
            "search path is unsafe",
        )

    total_read = 0
    folded_pattern = pattern.casefold()
    for relative in files:
        try:
            state = workspace_files.read_regular_bytes_anchored(
                root,
                relative,
                max_bytes=MAX_WORKSPACE_FILE_BYTES,
                expected_root_identity=root_identity,
            )
        except workspace_files.WorkspaceIOError as exc:
            if exc.code == "workspace_file_limit_exceeded":
                raise workspace_files.WorkspaceIOError(
                    "workspace_search_limit_exceeded",
                    "workspace search file limit exceeded",
                ) from exc
            raise
        if not state["exists"]:
            continue
        total_read += len(state["data"])
        if total_read > MAX_WORKSPACE_SEARCH_BYTES:
            raise workspace_files.WorkspaceIOError(
                "workspace_search_limit_exceeded",
                "workspace search byte limit exceeded",
            )
        text = _decode_workspace_utf8(state["data"])
        for number, line in enumerate(text.splitlines(), start=1):
            if folded_pattern in line.casefold():
                matches.append(f"{relative}:{number}:{line}")
                if len(matches) >= limit:
                    return matches
    return matches


def _safe_env_template_files(context, directory):
    root = Path(context.root)
    root_identity = _workspace_root_identity(context)
    templates = []
    stack = [(directory, 0)]
    scanned = 0
    while stack:
        current, depth = stack.pop()
        listing = workspace_files.list_directory_names_anchored(
            root,
            current,
            max_entries=MAX_WORKSPACE_DIRECTORY_ENTRIES,
            expected_root_identity=root_identity,
        )
        scanned += listing["scanned"]
        if scanned > MAX_WORKSPACE_SEARCH_FILES:
            raise workspace_files.WorkspaceIOError(
                "workspace_search_limit_exceeded",
                "workspace search entry limit exceeded",
            )
        children = []
        for entry in listing["entries"]:
            relative = entry["name"] if current == "." else f"{current}/{entry['name']}"
            if entry["name"] in IGNORED_PATH_NAMES:
                continue
            if stat.S_ISDIR(entry["mode"]):
                if entry["name"].casefold() in _ALLOWED_ENV_TEMPLATES:
                    continue
                if security_paths.is_sensitive_path(relative):
                    continue
                if depth >= MAX_WORKSPACE_SEARCH_DEPTH:
                    raise workspace_files.WorkspaceIOError(
                        "workspace_search_limit_exceeded",
                        "workspace search depth limit exceeded",
                    )
                children.append(relative)
            elif (
                stat.S_ISREG(entry["mode"])
                and entry["name"].casefold() in _ALLOWED_ENV_TEMPLATES
            ):
                templates.append(root / relative)
        for child in reversed(children):
            stack.append((child, depth + 1))
    templates.sort(key=lambda path: path.as_posix().casefold())
    return templates


def _relative_search_path(root, raw_path):
    raw = str(raw_path)
    if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        return None
    root = Path(root).resolve()
    source = Path(raw)
    candidate = Path(
        os.path.abspath(os.fspath(source if source.is_absolute() else root / source))
    )
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    relative_text = relative.as_posix()
    if security_paths.is_sensitive_path(relative_text):
        return None
    return relative_text


def _filter_rg_output(root, output):
    remaining = str(output or "")
    matches = []
    while remaining:
        raw_path, separator, after_path = remaining.partition("\x00")
        if not separator:
            break
        record, newline, remaining = after_path.partition("\n")
        if not newline:
            remaining = ""
        line_number, colon, body = record.partition(":")
        if not colon or not line_number.isdigit():
            continue
        relative = _relative_search_path(root, raw_path)
        if relative is None:
            continue
        matches.append(f"{relative}:{line_number}:{body.rstrip(chr(13))}")
        if len(matches) >= 200:
            break
    return "\n".join(matches) or "(no matches)"
