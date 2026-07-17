"""Repository-local ``.env`` parsing and atomic updates."""

import json
import os
import re
import stat
import sys
from pathlib import Path

from pico.security.private_files import (
    ensure_private_dir,
    private_directory_identity,
    read_private_text,
    write_private_bytes_atomic,
)
from pico.state.file_lock import locked_file


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_PROJECT_ENV_BYTES = 1024 * 1024


def _strip_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON-quoted .env value") from exc
        if not isinstance(decoded, str):
            raise ValueError("quoted .env value must be a string")
        return decoded
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def _strip_inline_comment(value):
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if char in {"'", '"'}:
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            continue
        if char == "#" and not quote and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def _parse_env_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].strip()
    if "=" not in line:
        raise ValueError("missing '=' separator")
    name, value = line.split("=", 1)
    name = name.strip()
    if not ENV_KEY_PATTERN.match(name):
        raise ValueError("invalid .env variable name")
    value = _strip_quotes(_strip_inline_comment(value))
    if any(char in value for char in ("\0", "\r", "\n")):
        raise ValueError("invalid control character in .env value")
    return name, value


def project_env_path(workspace_root):
    return Path(workspace_root).resolve() / ".env"


def project_env_metadata(workspace_root, status):
    return {
        "path": str(project_env_path(workspace_root)),
        "scope": "repo_root_exact",
        "status": str(status),
    }


def _warn_invalid_env_line(env_path, line_number, error):
    print(f"warning: skipped invalid .env line {line_number}: {error}", file=sys.stderr)


def read_project_env_with_status(start, warn=True):
    env_path = project_env_path(start)
    try:
        initial_mode = env_path.lstat().st_mode
        text = read_private_text(env_path, max_bytes=MAX_PROJECT_ENV_BYTES)
    except FileNotFoundError:
        return {}, project_env_metadata(start, "missing")
    loaded = {}
    status = (
        "review_required"
        if os.name == "posix" and stat.S_IMODE(initial_mode) != 0o600
        else "loaded"
    )
    for line_number, line in enumerate(text.splitlines(), start=1):
        try:
            parsed = _parse_env_line(line)
        except ValueError as exc:
            status = "review_required"
            if warn:
                _warn_invalid_env_line(env_path, line_number, exc)
            continue
        if parsed is None:
            continue
        name, value = parsed
        loaded[name] = value
    return loaded, project_env_metadata(start, status)


def read_project_env(start, warn=True):
    loaded, _ = read_project_env_with_status(start, warn=warn)
    return loaded


def _validated_project_env_assignments(assignments):
    result = {}
    for raw_name, raw_value in dict(assignments or {}).items():
        name = str(raw_name)
        if not ENV_KEY_PATTERN.fullmatch(name):
            raise ValueError("invalid project environment variable name")
        value = "" if raw_value is None else str(raw_value)
        if any(char in value for char in ("\0", "\r", "\n")):
            raise ValueError(f"{name} cannot contain NUL or newlines")
        result[name] = value
    return result


def _format_project_env_assignment(name, value):
    return f"{name}={json.dumps(value, ensure_ascii=False)}"


def _render_project_env_update(existing_text, assignments):
    existing_lines = existing_text.splitlines()
    rendered = []
    seen = set()
    old_values = {}
    for line in existing_lines:
        try:
            parsed = _parse_env_line(line)
        except ValueError:
            rendered.append(line)
            continue
        if parsed is None:
            rendered.append(line)
            continue
        name, old_value = parsed
        if name not in assignments:
            rendered.append(line)
            continue
        old_values.setdefault(name, []).append(old_value)
        if name not in seen:
            rendered.append(_format_project_env_assignment(name, assignments[name]))
            seen.add(name)

    added = [name for name in assignments if name not in seen]
    if added and rendered and rendered[-1].strip():
        rendered.append("")
    rendered.extend(
        _format_project_env_assignment(name, assignments[name]) for name in added
    )
    updated = [
        name
        for name in assignments
        if name in seen
        and (len(old_values[name]) != 1 or old_values[name][0] != assignments[name])
    ]
    unchanged = [name for name in assignments if name in seen and name not in updated]
    text = "\n".join(rendered)
    return (text + "\n" if rendered else ""), {
        "updated": updated,
        "added": added,
        "unchanged": unchanged,
    }


def write_project_env_assignments(workspace_root, assignments):
    assignments = _validated_project_env_assignments(assignments)
    root = Path(workspace_root).resolve()
    root_identity = private_directory_identity(root)
    private_root = ensure_private_dir(root / ".pico")
    env_path = project_env_path(root)
    lock_path = private_root / "project-env.lock"

    with locked_file(lock_path, require_lock=True):
        try:
            existing_text = read_private_text(
                env_path,
                trusted_root=root,
                trusted_root_identity=root_identity,
                max_bytes=MAX_PROJECT_ENV_BYTES,
            )
        except FileNotFoundError:
            existing_text = ""
        content, result = _render_project_env_update(existing_text, assignments)
        rendered = content.encode("utf-8")
        if len(rendered) > MAX_PROJECT_ENV_BYTES:
            raise ValueError("private file too large")
        write_private_bytes_atomic(
            env_path,
            rendered,
            trusted_root=root,
            trusted_root_identity=root_identity,
            error="project env temp changed",
            max_existing_bytes=MAX_PROJECT_ENV_BYTES,
        )
    return result
