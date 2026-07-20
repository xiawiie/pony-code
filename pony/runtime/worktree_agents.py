"""Isolated Git worktrees for bounded parallel child-agent tasks."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import uuid

from pony.agent.verification import is_verification_argv
from pony.security import paths as security_paths
from pony.security.private_files import (
    ensure_private_dir,
    private_directory_identity,
    read_private_text,
    write_private_bytes_atomic,
)
from pony.state.run_store import RunStore
from pony.state.session_store import SessionStore
from pony.tools.permissions import PermissionMode
from pony.tools.subprocess import run_hardened_git
from pony.workspace.context import WorkspaceContext


MAX_MANIFEST_BYTES = 64 * 1024
MAX_BATCH_MANIFEST_BYTES = 1024 * 1024
MAX_GIT_STATUS_BYTES = 4 * 1024 * 1024
MAX_RESULT_CHARS = 2_000
_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}-[0-9a-f]{12}$")
_BATCH_ID_RE = re.compile(r"^batch-[0-9a-f]{12}$")
_BRANCH_RE = re.compile(r"^codex/pony-agent-[a-z][a-z0-9_-]{0,63}-[0-9a-f]{12}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_TERMINAL_STATUSES = {"completed", "stopped", "failed", "ready", "merged", "cleaned"}
_TEST_STATUSES = {"not_run", "blocked", "passed", "failed"}
_DIFF_STATUSES = {"unknown", "clean", "dirty"}
_CHILD_READ_TOOLS = ("list_files", "read_file", "search", "repo_lookup")
_CHILD_WRITE_TOOLS = (
    *_CHILD_READ_TOOLS,
    "run_shell",
    "write_file",
    "patch_file",
)
_COMMIT_IDENTITY = ("Pony Agent", "pony-agent@localhost")
_WORKTREE_PATHSPECS = (".", ":(exclude).pony/**")


def _git(git, cwd, args, *, check=True, text=True, timeout=60, commit=False):
    return run_hardened_git(
        git,
        args,
        cwd=cwd,
        check=check,
        text=text,
        timeout=timeout,
        commit_identity=_COMMIT_IDENTITY if commit else None,
    )


def _store_root(source_root):
    return Path(source_root).resolve(strict=True) / ".pony" / "worktree-agents"


def _agent_root(source_root, agent_id):
    agent_id = str(agent_id or "")
    if _AGENT_ID_RE.fullmatch(agent_id) is None:
        raise ValueError("invalid worktree agent id")
    return _store_root(source_root) / agent_id


def _batch_root(source_root, batch_id):
    batch_id = str(batch_id or "")
    if _BATCH_ID_RE.fullmatch(batch_id) is None:
        raise ValueError("invalid worktree agent batch id")
    return _store_root(source_root) / "batches" / batch_id


def _write_manifest(agent_root, manifest):
    agent_root = ensure_private_dir(agent_root)
    root_identity = private_directory_identity(agent_root)
    data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("worktree agent manifest is too large")
    write_private_bytes_atomic(
        agent_root / "manifest.json",
        data,
        trusted_root=agent_root,
        trusted_root_identity=root_identity,
        max_existing_bytes=MAX_MANIFEST_BYTES,
        error="worktree agent manifest changed",
    )


def _write_batch_manifest(batch_root, manifest):
    batch_root = ensure_private_dir(batch_root)
    root_identity = private_directory_identity(batch_root)
    data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MAX_BATCH_MANIFEST_BYTES:
        raise ValueError("worktree agent batch manifest is too large")
    write_private_bytes_atomic(
        batch_root / "manifest.json",
        data,
        trusted_root=batch_root,
        trusted_root_identity=root_identity,
        max_existing_bytes=MAX_BATCH_MANIFEST_BYTES,
        error="worktree agent batch manifest changed",
    )


def _validated_manifest(agent_root, value):
    if not isinstance(value, dict):
        raise ValueError("invalid worktree agent manifest")
    required = {
        "format_version",
        "id",
        "name",
        "mode",
        "branch",
        "worktree_rel",
        "base_commit",
        "status",
        "changed_files",
        "diff_status",
        "test_status",
        "branch_head",
    }
    if not required <= set(value):
        raise ValueError("invalid worktree agent manifest")
    agent_id = value["id"]
    if (
        value["format_version"] != 1
        or _AGENT_ID_RE.fullmatch(str(agent_id or "")) is None
        or Path(agent_root).name != agent_id
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", str(value["name"] or ""))
        is None
        or _BRANCH_RE.fullmatch(str(value["branch"] or "")) is None
        or value["worktree_rel"] != f".pony/worktree-agents/{agent_id}/worktree"
        or _COMMIT_RE.fullmatch(str(value["base_commit"] or "")) is None
        or value["mode"] not in {"readonly", "write"}
        or value["status"]
        not in {"created", "running", *_TERMINAL_STATUSES}
        or type(value["changed_files"]) is not int
        or not 0 <= value["changed_files"] <= 100_000
        or value["diff_status"] not in _DIFF_STATUSES
        or value["test_status"] not in _TEST_STATUSES
        or (
            value["branch_head"] != ""
            and _COMMIT_RE.fullmatch(str(value["branch_head"] or "")) is None
        )
        or (
            "batch_id" in value
            and _BATCH_ID_RE.fullmatch(str(value["batch_id"] or "")) is None
        )
    ):
        raise ValueError("invalid worktree agent manifest")
    return dict(value)


def load_worktree_agent(source_root, agent_id):
    agent_root = _agent_root(source_root, agent_id)
    root_identity = private_directory_identity(agent_root)
    raw = read_private_text(
        agent_root / "manifest.json",
        trusted_root=agent_root,
        trusted_root_identity=root_identity,
        max_bytes=MAX_MANIFEST_BYTES,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("invalid worktree agent manifest") from None
    return _validated_manifest(agent_root, value)


def list_worktree_agents(source_root):
    root = _store_root(source_root)
    try:
        root_identity = private_directory_identity(root)
    except FileNotFoundError:
        return []
    candidates = []
    with os.scandir(root) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and _AGENT_ID_RE.fullmatch(entry.name):
                candidates.append((info.st_mtime_ns, entry.name))
    if len(candidates) > 100:
        raise ValueError("too many worktree agent manifests")
    if private_directory_identity(root) != root_identity:
        raise ValueError("worktree agent root changed")
    return [
        load_worktree_agent(source_root, agent_id)
        for _mtime, agent_id in sorted(candidates, reverse=True)
    ]


def _validated_batch_manifest(batch_root, value):
    if not isinstance(value, dict):
        raise ValueError("invalid worktree agent batch manifest")
    required = {"format_version", "id", "base_commit", "status", "children"}
    optional = {"merged_commit"}
    if not required <= set(value) or set(value) - required - optional:
        raise ValueError("invalid worktree agent batch manifest")
    batch_id = value["id"]
    children = value["children"]
    if (
        value["format_version"] != 1
        or _BATCH_ID_RE.fullmatch(str(batch_id or "")) is None
        or Path(batch_root).name != batch_id
        or _COMMIT_RE.fullmatch(str(value["base_commit"] or "")) is None
        or (
            "merged_commit" in value
            and _COMMIT_RE.fullmatch(str(value["merged_commit"] or "")) is None
        )
        or (value["status"] == "merged") != ("merged_commit" in value)
        or value["status"] not in {"running", "review_required", "merged", "cleaned"}
        or not isinstance(children, list)
        or not 1 <= len(children) <= 8
    ):
        raise ValueError("invalid worktree agent batch manifest")
    seen = set()
    validated_children = []
    for child in children:
        if not isinstance(child, dict):
            raise ValueError("invalid worktree agent batch manifest")
        child_required = {
            "id",
            "name",
            "branch",
            "base_commit",
            "branch_head",
            "status",
            "changed_files",
            "changed_paths",
            "test_status",
        }
        if (
            set(child) != child_required
            or _AGENT_ID_RE.fullmatch(str(child.get("id") or "")) is None
            or child["id"] in seen
            or re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_-]{0,63}", str(child.get("name") or "")
            )
            is None
            or _BRANCH_RE.fullmatch(str(child.get("branch") or "")) is None
            or child["base_commit"] != value["base_commit"]
            or (
                child["branch_head"] != ""
                and _COMMIT_RE.fullmatch(str(child["branch_head"] or "")) is None
            )
            or child["status"] not in _TERMINAL_STATUSES | {"created", "running"}
            or type(child["changed_files"]) is not int
            or not 0 <= child["changed_files"] <= 100_000
            or not isinstance(child["changed_paths"], list)
            or len(child["changed_paths"]) > 1_000
            or any(
                not isinstance(path, str)
                or not path
                or len(path) > 4_096
                or "\x00" in path
                or Path(path).is_absolute()
                or ".." in Path(path).parts
                for path in child["changed_paths"]
            )
            or child["test_status"] not in _TEST_STATUSES
        ):
            raise ValueError("invalid worktree agent batch manifest")
        seen.add(child["id"])
        validated_children.append(dict(child))
    result = dict(value)
    result["children"] = validated_children
    return result


def load_worktree_agent_batch(source_root, batch_id):
    batch_root = _batch_root(source_root, batch_id)
    root_identity = private_directory_identity(batch_root)
    raw = read_private_text(
        batch_root / "manifest.json",
        trusted_root=batch_root,
        trusted_root_identity=root_identity,
        max_bytes=MAX_BATCH_MANIFEST_BYTES,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("invalid worktree agent batch manifest") from None
    return _validated_batch_manifest(batch_root, value)


def list_worktree_agent_batches(source_root):
    root = _store_root(source_root) / "batches"
    try:
        root_identity = private_directory_identity(root)
    except FileNotFoundError:
        return []
    candidates = []
    with os.scandir(root) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and _BATCH_ID_RE.fullmatch(entry.name):
                candidates.append((info.st_mtime_ns, entry.name))
    if private_directory_identity(root) != root_identity:
        raise ValueError("worktree agent batch root changed")
    return [
        load_worktree_agent_batch(source_root, batch_id)
        for _mtime, batch_id in sorted(candidates, reverse=True)[:100]
    ]


def _status_records(raw):
    if len(raw or b"") > MAX_GIT_STATUS_BYTES:
        raise ValueError("worktree agent Git status is too large")
    records = bytes(raw or b"").split(b"\x00")
    if records and records[-1] == b"":
        records.pop()
    paths = []
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 4 or record[2:3] != b" ":
            raise ValueError("invalid Git status output")
        paths.append(record[3:])
        renamed = record[:1] in {b"R", b"C"} or record[1:2] in {b"R", b"C"}
        if renamed:
            index += 1
            if index >= len(records):
                raise ValueError("invalid Git rename status")
            paths.append(records[index])
        index += 1
    return paths


def _worktree_snapshot(git, worktree):
    status = _git(
        git,
        worktree,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *_WORKTREE_PATHSPECS,
        ],
        text=False,
    ).stdout
    paths = _status_records(status)
    return {
        "changed_files": len(set(paths)),
        "diff_status": "dirty" if paths else "clean",
    }


def _test_status(messages):
    messages = list(messages or [])
    last_change = -1
    for index, message in enumerate(messages):
        metadata = message.get("_pony_meta", {}) if isinstance(message, dict) else {}
        if metadata.get("workspace_changed") is True:
            last_change = index
    statuses = []
    for index, message in enumerate(messages[:-1]):
        content = message.get("content") if isinstance(message, dict) else None
        if (
            message.get("role") != "assistant"
            or not isinstance(content, list)
            or len(content) != 1
            or content[0].get("type") != "tool_use"
            or content[0].get("name") != "run_shell"
        ):
            continue
        command = str(content[0].get("input", {}).get("command", ""))
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        if not is_verification_argv(argv):
            continue
        result = messages[index + 1]
        metadata = result.get("_pony_meta", {}) if isinstance(result, dict) else {}
        if index + 1 <= last_change:
            continue
        statuses.append(str(metadata.get("tool_status", "")))
    if any(status in {"error", "partial_success"} for status in statuses):
        return "failed"
    if any(status == "ok" for status in statuses):
        return "passed"
    if statuses:
        return "blocked"
    return "not_run"


def _new_manifest(parent, item, base_commit, batch_id):
    suffix = uuid.uuid4().hex[:12]
    name = str(item["name"]).strip()
    slug = name.casefold()
    agent_id = f"{slug}-{suffix}"
    branch = f"codex/pony-agent-{slug}-{suffix}"
    manifest = {
        "format_version": 1,
        "id": agent_id,
        "name": name,
        "mode": item.get("mode", "readonly"),
        "branch": branch,
        "worktree_rel": f".pony/worktree-agents/{agent_id}/worktree",
        "base_commit": base_commit,
        "batch_id": batch_id,
        "status": "created",
        "changed_files": 0,
        "changed_paths": [],
        "diff_status": "unknown",
        "test_status": "not_run",
        "branch_head": "",
        "session_id": "",
        "run_id": "",
        "summary": "",
        "error": "",
    }
    return parent.redact_artifact(manifest), _agent_root(parent.source_root, agent_id)


def _remove_setup(git, source_root, manifest):
    worktree = Path(source_root) / manifest["worktree_rel"]
    _git(
        git,
        source_root,
        ["worktree", "remove", "--force", str(worktree)],
        check=False,
    )
    _git(
        git,
        source_root,
        ["branch", "-D", manifest["branch"]],
        check=False,
    )
    listed = _git(git, source_root, ["worktree", "list", "--porcelain"]).stdout
    branches = _git(
        git,
        source_root,
        ["branch", "--list", manifest["branch"]],
    ).stdout
    if f"worktree {worktree}\n" in listed or branches.strip():
        raise RuntimeError("failed to clean up worktree agent setup")
    try:
        mode = worktree.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError("unsafe worktree agent setup residue")
    shutil.rmtree(worktree)


def _prepare_worktree(parent, git, item, base_commit, batch_id):
    manifest, agent_root = _new_manifest(parent, item, base_commit, batch_id)
    ensure_private_dir(agent_root)
    worktree = agent_root / "worktree"
    try:
        _git(
            git,
            parent.source_root,
            [
                "worktree",
                "add",
                "-b",
                manifest["branch"],
                str(worktree),
                base_commit,
            ],
        )
        if stat.S_ISLNK(worktree.lstat().st_mode) or not worktree.is_dir():
            raise ValueError("Git created an unsafe worktree")
        _write_manifest(agent_root, manifest)
        return {"item": dict(item), "manifest": manifest, "root": agent_root}
    except Exception:
        _remove_setup(git, parent.source_root, manifest)
        shutil.rmtree(agent_root)
        raise


def _build_child(parent, prepared, client):
    from pony.runtime.options import RuntimeOptions

    manifest = prepared["manifest"]
    item = prepared["item"]
    worktree = prepared["root"] / "worktree"
    workspace = WorkspaceContext.build(
        worktree,
        executables=parent.trusted_executables,
        repo_root_override=worktree,
    )
    session_id = f"worktree-{manifest['id']}"
    child = parent.__class__(
        model_client=client,
        workspace=workspace,
        session_store=SessionStore(worktree / ".pony" / "sessions"),
        options=RuntimeOptions(
            run_store=RunStore(worktree / ".pony" / "runs"),
            project_trusted=parent.project_trusted,
            max_steps=int(item.get("max_steps", 6)),
            max_output_tokens=parent.max_output_tokens,
            context_window=parent.model_capabilities.context_window,
            depth=parent.depth + 1,
            max_depth=parent.depth + 1,
            read_only=manifest["mode"] == "readonly",
            secret_env_names=parent.secret_env_names,
            redaction_env=parent.redaction_env,
            trusted_redaction_env=True,
            trusted_executables=parent.trusted_executables,
            shell_env_allowlist=parent.shell_env_allowlist,
            project_config=deepcopy(parent.project_config),
            allowed_tools=(
                _CHILD_READ_TOOLS
                if manifest["mode"] == "readonly"
                else _CHILD_WRITE_TOOLS
            ),
            session_id=session_id,
        ),
    )
    child.set_permission_mode(
        PermissionMode.DONT_ASK.value
        if manifest["mode"] == "readonly"
        else PermissionMode.ACCEPT_EDITS.value
    )
    child._approval_prompt = lambda _name, _args: False
    manifest["session_id"] = session_id
    _write_manifest(prepared["root"], manifest)
    return child


def _run_child(parent, git, prepared, child):
    manifest = prepared["manifest"]
    item = prepared["item"]
    manifest["status"] = "running"
    _write_manifest(prepared["root"], manifest)
    try:
        result = child.ask(str(item["task"]).strip())
        task_state = child.current_task_state
        manifest["status"] = (
            "completed" if task_state.status == "completed" else "stopped"
        )
        manifest["summary"] = parent.redact_text(result)[:MAX_RESULT_CHARS]
        manifest["run_id"] = str(task_state.run_id)
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = parent.redact_text(str(exc))[:300]
        task_state = getattr(child, "current_task_state", None)
        manifest["run_id"] = str(getattr(task_state, "run_id", "") or "")
    try:
        manifest.update(_seal_worktree(git, prepared))
    except Exception as exc:
        manifest["status"] = "failed"
        if not manifest["error"]:
            detail = parent.redact_text(str(exc))[:240]
            manifest["error"] = f"worktree finalization failed: {detail}"
        _write_manifest(prepared["root"], parent.redact_artifact(manifest))
        return dict(manifest)
    manifest["test_status"] = _test_status(child.session.get("messages", []))
    _write_manifest(prepared["root"], parent.redact_artifact(manifest))
    return dict(manifest)


def _clean_parent_head(git, source_root):
    if not _git(git, source_root, ["branch", "--show-current"]).stdout.strip():
        raise ValueError("worktree agents require a checked-out parent branch")
    base = _git(git, source_root, ["rev-parse", "HEAD"]).stdout.strip()
    if _COMMIT_RE.fullmatch(base) is None:
        raise ValueError("worktree agents require a valid Git HEAD")
    status = _git(
        git,
        source_root,
        [
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *_WORKTREE_PATHSPECS,
        ],
    ).stdout
    if status:
        raise ValueError("worktree agents require a clean parent worktree")
    return base


def _batch_child_evidence(manifest):
    return {
        key: manifest[key]
        for key in (
            "id",
            "name",
            "branch",
            "base_commit",
            "branch_head",
            "status",
            "changed_files",
            "changed_paths",
            "test_status",
        )
    }


def _batch_evidence_matches(evidence, manifest):
    current = _batch_child_evidence(manifest)
    if current["status"] in {"merged", "cleaned"}:
        current["status"] = "completed"
    return current == evidence


def run_worktree_agents(parent, args):
    parent.validate_tool("delegate_worktrees", args)
    if not callable(parent.delegate_model_client_factory):
        raise ValueError("delegate model client factory is not configured")
    git = parent.trusted_executables.get("git")
    if not git:
        raise ValueError("trusted Git executable is unavailable")
    tasks = [dict(item) for item in args["tasks"]]
    base_commit = _clean_parent_head(git, parent.source_root)
    batch_id = f"batch-{uuid.uuid4().hex[:12]}"
    batch_root = _batch_root(parent.source_root, batch_id)
    prepared = []
    try:
        for item in tasks:
            prepared.append(_prepare_worktree(parent, git, item, base_commit, batch_id))
        if _clean_parent_head(git, parent.source_root) != base_commit:
            raise ValueError("parent HEAD changed while creating worktrees")
        clients = [parent.delegate_model_client_factory() for _item in prepared]
        if any(client is parent.model_client for client in clients) or len(
            {id(client) for client in clients}
        ) != len(clients):
            raise ValueError("delegate model client factory reused a client")
        children = [
            _build_child(parent, item, client)
            for item, client in zip(prepared, clients, strict=True)
        ]
        _write_batch_manifest(
            batch_root,
            {
                "format_version": 1,
                "id": batch_id,
                "base_commit": base_commit,
                "status": "running",
                "children": [
                    _batch_child_evidence(item["manifest"]) for item in prepared
                ],
            },
        )
    except Exception:
        cleanup_errors = []
        for item in reversed(prepared):
            try:
                _remove_setup(git, parent.source_root, item["manifest"])
                shutil.rmtree(item["root"])
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if batch_root.exists():
            try:
                shutil.rmtree(batch_root)
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise RuntimeError("worktree agent setup cleanup failed") from cleanup_errors[0]
        raise

    results = [None] * len(prepared)
    with ThreadPoolExecutor(
        max_workers=int(args.get("max_parallel", 2)),
        thread_name_prefix="pony-worktree-agent",
    ) as pool:
        futures = {
            pool.submit(_run_child, parent, git, item, child): index
            for index, (item, child) in enumerate(zip(prepared, children, strict=True))
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    _write_batch_manifest(
        batch_root,
        {
            "format_version": 1,
            "id": batch_id,
            "base_commit": base_commit,
            "status": "review_required",
            "children": [_batch_child_evidence(result) for result in results],
        },
    )
    return _render_tool_result(batch_id, results)


def _render_tool_result(batch_id, results):
    lines = [
        "worktree agents completed; no branches were merged",
        f"batch: {batch_id}",
    ]
    for result in results:
        lines.extend(
            (
                f"- {result['name']} ({result['id']}): {result['status']}",
                f"  branch: {result['branch']}",
                f"  worktree: {result['worktree_rel']}",
                f"  diff: {result['diff_status']} ({result['changed_files']} files)",
                f"  tests: {result['test_status']}",
                f"  merge: pony agents merge {result['id']}",
            )
        )
    return "\n".join(lines)


def _live_worktree(source_root, manifest):
    return Path(source_root).resolve(strict=True) / manifest["worktree_rel"]


def inspect_worktree_agent(source_root, agent_id, git=None):
    manifest = load_worktree_agent(source_root, agent_id)
    worktree = _live_worktree(source_root, manifest)
    result = dict(manifest)
    result["worktree"] = str(worktree)
    if git and manifest["status"] != "cleaned" and worktree.exists():
        snapshot = _worktree_snapshot(git, worktree)
        result["worktree_changed_files"] = snapshot["changed_files"]
        result["worktree_diff_status"] = snapshot["diff_status"]
    return result


def inspect_worktree_agent_batch(source_root, batch_id, git=None):
    batch = load_worktree_agent_batch(source_root, batch_id)
    result = dict(batch)
    children = []
    changed_paths = {}
    for evidence in batch["children"]:
        manifest = load_worktree_agent(source_root, evidence["id"])
        child = _batch_child_evidence(manifest)
        child["sealed_evidence_matches"] = _batch_evidence_matches(evidence, manifest)
        if git:
            for path in child["changed_paths"]:
                changed_paths.setdefault(path, []).append(child["id"])
        children.append(child)
    result["children"] = children
    result["overlapping_paths"] = {
        path: agent_ids
        for path, agent_ids in sorted(changed_paths.items())
        if len(agent_ids) > 1
    }
    return result


def _validate_change_path(root, relative_text):
    if not relative_text or "\x00" in relative_text:
        raise ValueError("unsafe worktree agent change")
    relative = Path(relative_text)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe worktree agent change")
    if relative.parts[0].casefold() == ".pony":
        raise ValueError("worktree agent change targets product state")
    if security_paths.is_sensitive_path(relative.as_posix().casefold()):
        raise ValueError("worktree agent change targets a sensitive path")
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("worktree agent change contains a symlink")
        if current == root / relative and (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
        ):
            raise ValueError("worktree agent change is not a regular file")


def _committed_change_paths(git, worktree, base_commit, branch_head):
    raw = _git(
        git,
        worktree,
        [
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            base_commit,
            branch_head,
            "--",
            ".",
        ],
        text=False,
    ).stdout
    if len(raw or b"") > MAX_GIT_STATUS_BYTES:
        raise ValueError("worktree agent Git diff is too large")
    paths = [os.fsdecode(path) for path in bytes(raw or b"").split(b"\x00") if path]
    if len(paths) > 1_000:
        raise ValueError("worktree agent changed too many files")
    return paths


def _ensure_safe_changes(git, worktree):
    raw = _git(
        git,
        worktree,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *_WORKTREE_PATHSPECS,
        ],
        text=False,
    ).stdout
    root = Path(worktree).resolve(strict=True)
    changed_paths = []
    for raw_path in _status_records(raw):
        relative_text = os.fsdecode(raw_path)
        _validate_change_path(root, relative_text)
        if relative_text not in changed_paths:
            changed_paths.append(relative_text)
    if len(changed_paths) > 1_000:
        raise ValueError("worktree agent changed too many files")
    return changed_paths


def _seal_worktree(git, prepared):
    """Commit the terminal child diff so later edits cannot enter its merge."""
    manifest = prepared["manifest"]
    worktree = prepared["root"] / "worktree"
    changed_paths = _ensure_safe_changes(git, worktree)
    for index in range(0, len(changed_paths), 100):
        _git(
            git,
            worktree,
            ["add", "-A", "--", *changed_paths[index : index + 100]],
        )
    staged = _git(git, worktree, ["diff", "--cached", "--quiet"], check=False)
    if staged.returncode not in {0, 1}:
        raise ValueError("failed to inspect staged worktree agent changes")
    if staged.returncode == 1:
        _git(
            git,
            worktree,
            [
                "commit",
                "--no-verify",
                "-m",
                f"agent({manifest['name']}): isolated worktree task",
            ],
            commit=True,
        )
    branch_head = _git(git, worktree, ["rev-parse", "HEAD"]).stdout.strip()
    if _COMMIT_RE.fullmatch(branch_head) is None or not _is_ancestor(
        git,
        worktree,
        manifest["base_commit"],
        branch_head,
    ):
        raise ValueError("worktree agent branch head is invalid")
    committed_paths = _committed_change_paths(
        git,
        worktree,
        manifest["base_commit"],
        branch_head,
    )
    for relative_text in committed_paths:
        _validate_change_path(worktree, relative_text)
    snapshot = _worktree_snapshot(git, worktree)
    if snapshot["changed_files"]:
        raise ValueError("worktree agent changed during finalization")
    return {
        "branch_head": branch_head,
        "changed_files": len(committed_paths),
        "changed_paths": committed_paths,
        "diff_status": "dirty" if committed_paths else "clean",
    }


def _is_ancestor(git, cwd, ancestor, descendant):
    result = _git(
        git,
        cwd,
        ["merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise ValueError("Git ancestry check failed")
    return result.returncode == 0


def merge_worktree_agent(source_root, agent_id, git):
    manifest = load_worktree_agent(source_root, agent_id)
    if manifest["status"] not in _TERMINAL_STATUSES - {"merged", "cleaned"}:
        raise ValueError("worktree agent is not ready to merge")
    source_root = Path(source_root).resolve(strict=True)
    worktree = _live_worktree(source_root, manifest)
    if _git(git, worktree, ["branch", "--show-current"]).stdout.strip() != manifest[
        "branch"
    ]:
        raise ValueError("worktree agent branch changed")
    if not _is_ancestor(
        git,
        worktree,
        manifest["base_commit"],
        manifest["branch"],
    ):
        raise ValueError("worktree agent branch no longer contains its base")
    branch_head = _git(git, worktree, ["rev-parse", "HEAD"]).stdout.strip()
    if branch_head != manifest["branch_head"]:
        raise ValueError("worktree agent branch changed after completion")
    if _worktree_snapshot(git, worktree)["changed_files"]:
        raise ValueError("worktree agent has changes after completion")

    _clean_parent_head(git, source_root)
    merge_head = _git(
        git,
        source_root,
        ["rev-parse", "-q", "--verify", "MERGE_HEAD"],
        check=False,
    )
    if merge_head.returncode == 0:
        raise ValueError("parent repository already has a merge in progress")
    if _is_ancestor(git, source_root, branch_head, "HEAD"):
        manifest.update(status="merged", merged_commit=branch_head)
        _write_manifest(_agent_root(source_root, agent_id), manifest)
        return dict(manifest)
    preflight = _git(
        git,
        source_root,
        ["merge-tree", "--write-tree", "HEAD", branch_head],
        check=False,
    )
    if preflight.returncode != 0:
        raise ValueError("worktree agent merge has conflicts")
    merged = _git(
        git,
        source_root,
        ["merge", "--no-edit", "--no-ff", "--no-verify", branch_head],
        check=False,
        commit=True,
    )
    if merged.returncode != 0:
        aborted = _git(git, source_root, ["merge", "--abort"], check=False)
        if aborted.returncode != 0:
            raise RuntimeError("worktree agent merge failed and abort failed")
        raise ValueError("worktree agent merge failed and was aborted")
    manifest.update(
        status="merged",
        merged_commit=_git(git, source_root, ["rev-parse", "HEAD"]).stdout.strip(),
    )
    _write_manifest(_agent_root(source_root, agent_id), manifest)
    return dict(manifest)


def _validated_batch_children(source_root, batch, git, parent_head):
    children = []
    for evidence in batch["children"]:
        manifest = load_worktree_agent(source_root, evidence["id"])
        if not _batch_evidence_matches(evidence, manifest):
            raise ValueError("worktree agent batch evidence changed")
        if manifest["status"] not in {"completed", "merged", "cleaned"}:
            raise ValueError("worktree agent batch has an incomplete child")
        if manifest["test_status"] in {"blocked", "failed"}:
            raise ValueError("worktree agent batch has failed tests")
        if manifest["status"] in {"merged", "cleaned"}:
            if not _is_ancestor(git, source_root, manifest["branch_head"], parent_head):
                raise ValueError("worktree agent batch child is not merged into parent")
            children.append(manifest)
            continue
        worktree = _live_worktree(source_root, manifest)
        if _git(git, worktree, ["branch", "--show-current"]).stdout.strip() != manifest[
            "branch"
        ]:
            raise ValueError("worktree agent branch changed")
        if _git(git, worktree, ["rev-parse", "HEAD"]).stdout.strip() != manifest[
            "branch_head"
        ]:
            raise ValueError("worktree agent branch changed after completion")
        if _worktree_snapshot(git, worktree)["changed_files"]:
            raise ValueError("worktree agent has changes after completion")
        children.append(manifest)
    return children


def _preflight_batch_merge(source_root, children, git, parent_head):
    simulated_head = parent_head
    for manifest in children:
        branch_head = manifest["branch_head"]
        if _is_ancestor(git, source_root, branch_head, simulated_head):
            continue
        preflight = _git(
            git,
            source_root,
            ["merge-tree", "--write-tree", simulated_head, branch_head],
            check=False,
        )
        if preflight.returncode != 0:
            raise ValueError(
                f"worktree agent batch merge has conflicts at {manifest['id']}"
            )
        tree = preflight.stdout.splitlines()[0].strip()
        if _COMMIT_RE.fullmatch(tree) is None:
            raise ValueError("worktree agent batch merge preflight failed")
        simulated_head = _git(
            git,
            source_root,
            [
                "commit-tree",
                tree,
                "-p",
                simulated_head,
                "-p",
                branch_head,
                "-m",
                "Pony worktree agent batch preflight",
            ],
            commit=True,
        ).stdout.strip()
        if _COMMIT_RE.fullmatch(simulated_head) is None:
            raise ValueError("worktree agent batch merge preflight failed")


def merge_worktree_agent_batch(source_root, batch_id, git):
    source_root = Path(source_root).resolve(strict=True)
    batch = load_worktree_agent_batch(source_root, batch_id)
    if batch["status"] == "merged":
        parent_head = _clean_parent_head(git, source_root)
        if not _is_ancestor(git, source_root, batch["merged_commit"], parent_head):
            raise ValueError("worktree agent batch merge is not in parent history")
        return dict(batch)
    if batch["status"] != "review_required":
        raise ValueError("worktree agent batch is not ready to merge")
    parent_head = _clean_parent_head(git, source_root)
    merge_head = _git(
        git,
        source_root,
        ["rev-parse", "-q", "--verify", "MERGE_HEAD"],
        check=False,
    )
    if merge_head.returncode == 0:
        raise ValueError("parent repository already has a merge in progress")
    children = _validated_batch_children(source_root, batch, git, parent_head)
    _preflight_batch_merge(source_root, children, git, parent_head)

    expected_head = parent_head
    for manifest, evidence in zip(children, batch["children"], strict=True):
        if _clean_parent_head(git, source_root) != expected_head:
            raise ValueError("parent HEAD changed while merging worktree agent batch")
        current = load_worktree_agent(source_root, manifest["id"])
        if not _batch_evidence_matches(evidence, current):
            raise ValueError("worktree agent batch evidence changed")
        if current["status"] in {"merged", "cleaned"}:
            if not _is_ancestor(git, source_root, current["branch_head"], expected_head):
                raise ValueError("worktree agent batch child is not merged into parent")
            continue
        worktree = _live_worktree(source_root, current)
        if (
            _git(git, worktree, ["branch", "--show-current"]).stdout.strip()
            != current["branch"]
            or _git(git, worktree, ["rev-parse", "HEAD"]).stdout.strip()
            != current["branch_head"]
            or _worktree_snapshot(git, worktree)["changed_files"]
        ):
            raise ValueError("worktree agent changed while merging batch")
        if _is_ancestor(git, source_root, manifest["branch_head"], expected_head):
            continue
        merged = _git(
            git,
            source_root,
            ["merge", "--no-edit", "--no-ff", "--no-verify", manifest["branch_head"]],
            check=False,
            commit=True,
        )
        if merged.returncode != 0:
            aborted = _git(git, source_root, ["merge", "--abort"], check=False)
            if aborted.returncode != 0:
                raise RuntimeError("worktree agent batch merge failed and abort failed")
            raise ValueError("worktree agent batch merge failed and was aborted")
        expected_head = _git(git, source_root, ["rev-parse", "HEAD"]).stdout.strip()

    batch.update(status="merged", merged_commit=expected_head)
    _write_batch_manifest(_batch_root(source_root, batch_id), batch)
    return dict(batch)


def _reconcile_cleaned_batch(source_root, manifest):
    batch_id = manifest.get("batch_id")
    if not batch_id:
        return
    try:
        batch = load_worktree_agent_batch(source_root, batch_id)
    except FileNotFoundError:
        return
    if any(
        load_worktree_agent(source_root, child["id"])["status"] != "cleaned"
        for child in batch["children"]
    ):
        return
    batch.pop("merged_commit", None)
    batch["status"] = "cleaned"
    _write_batch_manifest(_batch_root(source_root, batch_id), batch)


def cleanup_worktree_agent(source_root, agent_id, git, *, discard=False):
    source_root = Path(source_root).resolve(strict=True)
    manifest = load_worktree_agent(source_root, agent_id)
    if manifest["status"] == "cleaned":
        _reconcile_cleaned_batch(source_root, manifest)
        return dict(manifest)
    if manifest["status"] in {"created", "running"} and not discard:
        raise ValueError("worktree agent is still running")
    if manifest["status"] not in _TERMINAL_STATUSES | {"created", "running"}:
        raise ValueError("worktree agent is not safe to clean up")
    worktree = _live_worktree(source_root, manifest)
    branch = _git(
        git,
        source_root,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{manifest['branch']}"],
        check=False,
    )
    if branch.returncode not in {0, 1}:
        raise ValueError("failed to inspect worktree agent branch")
    if not worktree.exists() and branch.returncode == 1:
        if manifest["status"] != "merged":
            raise ValueError("worktree agent branch is missing before merge")
        manifest["status"] = "cleaned"
        _write_manifest(_agent_root(source_root, agent_id), manifest)
        _reconcile_cleaned_batch(source_root, manifest)
        return dict(manifest)
    if worktree.exists() and not discard:
        snapshot = _worktree_snapshot(git, worktree)
        if snapshot["changed_files"]:
            raise ValueError("worktree agent has uncommitted changes")
    if not discard and not _is_ancestor(git, source_root, manifest["branch"], "HEAD"):
        raise ValueError("worktree agent branch has not been merged")
    if worktree.exists():
        _git(git, source_root, ["worktree", "remove", "--force", str(worktree)])
    deleted = _git(
        git,
        source_root,
        ["branch", "-D", manifest["branch"]],
        check=False,
    )
    remaining = _git(
        git,
        source_root,
        ["branch", "--list", manifest["branch"]],
    ).stdout
    if deleted.returncode != 0 and remaining.strip():
        raise ValueError("failed to remove worktree agent branch")
    manifest["status"] = "cleaned"
    _write_manifest(_agent_root(source_root, agent_id), manifest)
    _reconcile_cleaned_batch(source_root, manifest)
    return dict(manifest)
