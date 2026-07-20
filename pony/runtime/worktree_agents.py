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
MAX_GIT_STATUS_BYTES = 4 * 1024 * 1024
MAX_RESULT_CHARS = 2_000
_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}-[0-9a-f]{12}$")
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


def _new_manifest(parent, item, base_commit):
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
        "status": "created",
        "changed_files": 0,
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


def _prepare_worktree(parent, git, item, base_commit):
    manifest, agent_root = _new_manifest(parent, item, base_commit)
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


def run_worktree_agents(parent, args):
    parent.validate_tool("delegate_worktrees", args)
    if not callable(parent.delegate_model_client_factory):
        raise ValueError("delegate model client factory is not configured")
    git = parent.trusted_executables.get("git")
    if not git:
        raise ValueError("trusted Git executable is unavailable")
    tasks = [dict(item) for item in args["tasks"]]
    base_commit = _clean_parent_head(git, parent.source_root)
    prepared = []
    try:
        for item in tasks:
            prepared.append(_prepare_worktree(parent, git, item, base_commit))
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
    except Exception:
        cleanup_errors = []
        for item in reversed(prepared):
            try:
                _remove_setup(git, parent.source_root, item["manifest"])
                shutil.rmtree(item["root"])
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
    return _render_tool_result(results)


def _render_tool_result(results):
    lines = ["worktree agents completed; no branches were merged"]
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


def cleanup_worktree_agent(source_root, agent_id, git, *, discard=False):
    source_root = Path(source_root).resolve(strict=True)
    manifest = load_worktree_agent(source_root, agent_id)
    if manifest["status"] == "cleaned":
        return dict(manifest)
    if manifest["status"] not in _TERMINAL_STATUSES:
        raise ValueError("worktree agent is still running")
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
    return dict(manifest)
