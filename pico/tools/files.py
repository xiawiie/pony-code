"""Workspace file tool implementations."""

import stat

from pico.security import paths as security_paths
from pico.security import workspace_files as workspace_files
from pico.workspace.context import IGNORED_PATH_NAMES

from .validation import (
    MAX_WORKSPACE_DIRECTORY_ENTRIES,
    MAX_WORKSPACE_FILE_BYTES,
    MAX_WORKSPACE_LIST_RESULTS,
    SensitiveToolError,
    _anchored_tool_relative,
    _contains_sensitive_content,
    _decode_workspace_utf8,
    _encode_workspace_utf8,
    _read_workspace_file,
    _workspace_root_identity,
)


def tool_list_files(context, args):
    relative_directory = _anchored_tool_relative(
        context,
        args.get("path", "."),
        allow_root=True,
    )
    listing = workspace_files.list_directory_names_anchored(
        context.root,
        relative_directory,
        max_entries=MAX_WORKSPACE_DIRECTORY_ENTRIES,
        expected_root_identity=_workspace_root_identity(context),
    )
    lines = []
    for entry in listing["entries"]:
        name = entry["name"]
        if name in IGNORED_PATH_NAMES:
            continue
        relative = name if relative_directory == "." else f"{relative_directory}/{name}"
        if security_paths.is_sensitive_path(relative):
            lines.append(f"{name} [sensitive]")
            if len(lines) >= MAX_WORKSPACE_LIST_RESULTS:
                break
            continue
        mode = entry["mode"]
        if stat.S_ISLNK(mode):
            kind = "[L]"
        elif stat.S_ISDIR(mode):
            kind = "[D]"
        elif stat.S_ISREG(mode):
            kind = "[F]"
        else:
            kind = "[?]"
        lines.append(f"{kind} {relative}")
        if len(lines) >= MAX_WORKSPACE_LIST_RESULTS:
            break
    if listing["unsafe_count"]:
        lines.append(f"[unsafe skipped: {listing['unsafe_count']}]")
    return "\n".join(lines) or "(empty)"


def tool_read_file(context, args):
    relative, state = _read_workspace_file(context, args["path"])
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    if end - start + 1 > 200:
        raise ValueError("read_file accepts at most 200 lines")
    lines = _decode_workspace_utf8(state["data"]).splitlines()
    body = "\n".join(
        f"{number:>4}: {line}"
        for number, line in enumerate(lines[start - 1 : end], start=start)
    )
    return f"# {relative}\n{body}"


def tool_write_file(context, args):
    relative = _anchored_tool_relative(context, args["path"])
    content = str(args["content"])
    data = _encode_workspace_utf8(content)
    if _contains_sensitive_content(context, content):
        raise SensitiveToolError("sensitive_content_block")
    workspace_files.write_regular_bytes_anchored_atomic(
        context.root,
        relative,
        data,
        max_bytes=MAX_WORKSPACE_FILE_BYTES,
        expected_root_identity=_workspace_root_identity(context),
    )
    return f"wrote {relative} ({len(content)} chars)"


def tool_patch_file(context, args):
    relative, state = _read_workspace_file(context, args["path"])
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    text = _decode_workspace_utf8(state["data"])
    count = text.count(old_text)
    if count != 1:
        raise ValueError(f"old_text must occur exactly once, found {count}")
    candidate = text.replace(old_text, str(args["new_text"]), 1)
    data = _encode_workspace_utf8(candidate)
    if _contains_sensitive_content(context, candidate):
        raise SensitiveToolError("sensitive_content_block")
    workspace_files.write_regular_bytes_anchored_atomic(
        context.root,
        relative,
        data,
        max_bytes=MAX_WORKSPACE_FILE_BYTES,
        expected_sha256=state["sha256"],
        expected_root_identity=_workspace_root_identity(context),
    )
    return f"patched {relative}"
