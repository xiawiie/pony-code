import os
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from benchmarks.support.fake_provider import FakeModelClient
from pony import Pony
from pony.agent.context_manager import _convert_pony_tool_to_anthropic
from pony.cli.app import main
from pony.cli.errors import CliError
from pony.runtime.options import RuntimeOptions
from pony.runtime.worktree_agents import (
    cleanup_worktree_agent,
    list_worktree_agents,
    merge_worktree_agent,
)
from pony.state.session_store import SessionStore
from pony.tools.registry import WORKTREE_DELEGATE_TOOL_SPEC
from pony.workspace.context import WorkspaceContext


def _git(repo, *args, check=True):
    return subprocess.run(
        [shutil.which("git") or "git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / ".gitignore").write_text(".pony/\n", encoding="utf-8")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    return repo


def _agent(repo, clients, *, allowed_tools=None, read_only=False):
    pending = list(clients)
    return Pony(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(repo),
        session_store=SessionStore(repo / ".pony" / "sessions"),
        options=RuntimeOptions(
            project_trusted=True,
            delegate_model_client_factory=lambda: pending.pop(0),
            allowed_tools=allowed_tools,
            read_only=read_only,
        ),
    )


def _git_executable(agent):
    return agent.trusted_executables["git"]


def test_worktree_delegate_schema_is_one_typed_batch_tool():
    converted = _convert_pony_tool_to_anthropic(
        "delegate_worktrees", WORKTREE_DELEGATE_TOOL_SPEC
    )

    assert converted["input_schema"]["properties"]["tasks"]["type"] == "array"
    assert converted["input_schema"]["required"] == ["tasks"]
    task = converted["input_schema"]["properties"]["tasks"]["items"]
    assert task["required"] == ["name", "task"]
    assert task["additionalProperties"] is False


def test_write_agent_changes_only_its_independent_worktree(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(
        repo,
        [
            FakeModelClient(
                [
                    {
                        "name": "write_file",
                        "args": {"path": "child.txt", "content": "isolated\n"},
                    },
                    "done",
                ]
            )
        ],
    )

    result = agent.spawn_worktree_agents(
        {
            "tasks": [
                {"name": "writer", "task": "write child.txt", "mode": "write"}
            ],
            "max_parallel": 1,
        }
    )

    manifest = list_worktree_agents(repo)[0]
    worktree = repo / manifest["worktree_rel"]
    assert "no branches were merged" in result
    assert not (repo / "child.txt").exists()
    assert (worktree / "child.txt").read_text(encoding="utf-8") == "isolated\n"
    assert manifest["status"] == "completed"
    assert manifest["changed_files"] == 1
    assert manifest["test_status"] == "not_run"
    assert manifest["branch"].startswith("codex/pony-agent-writer-")
    assert list((worktree / ".pony" / "sessions").glob("*.jsonl"))
    assert list((worktree / ".pony" / "runs").glob("*"))


def test_model_tool_uses_the_normal_permission_gate(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(repo, [FakeModelClient(["done"])])
    args = {
        "tasks": [{"name": "reader", "task": "inspect"}],
        "max_parallel": 1,
    }

    denied = agent.execute_tool("delegate_worktrees", args)
    assert denied.metadata["tool_error_code"] == "permission_mode_block"

    agent.set_permission_rule("delegate_worktrees", "allow")
    allowed = agent.execute_tool("delegate_worktrees", args)
    assert allowed.metadata["tool_status"] == "ok"
    assert "no branches were merged" in allowed.content


def test_plan_and_allowed_tools_hide_worktree_delegate(tmp_path):
    repo = _repo(tmp_path)
    args = {
        "tasks": [{"name": "reader", "task": "inspect"}],
        "max_parallel": 1,
    }
    planned = _agent(repo, [FakeModelClient(["unused"])])
    planned.set_permission_mode("plan")
    planned._approval_prompt = lambda _name, _args: False
    planned.set_permission_rule("delegate_worktrees", "allow")

    assert "delegate_worktrees" not in planned.visible_tools()
    assert (
        planned.execute_tool("delegate_worktrees", args).metadata["tool_error_code"]
        == "approval_denied"
    )

    restricted = _agent(
        repo,
        [FakeModelClient(["unused"])],
        allowed_tools=("read_file",),
    )
    assert "delegate_worktrees" not in restricted.visible_tools()
    assert (
        restricted.execute_tool("delegate_worktrees", args).metadata["tool_error_code"]
        == "tool_not_allowed"
    )

    read_only = _agent(
        repo,
        [FakeModelClient(["unused"])],
        read_only=True,
    )
    assert "delegate_worktrees" not in read_only.visible_tools()
    assert (
        read_only.execute_tool("delegate_worktrees", args).metadata["tool_error_code"]
        == "read_only_block"
    )
    assert not list_worktree_agents(repo)


def test_readonly_agent_cannot_write_its_worktree(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(
        repo,
        [
            FakeModelClient(
                [
                    {
                        "name": "write_file",
                        "args": {"path": "blocked.txt", "content": "no"},
                    },
                    "done",
                ]
            )
        ],
    )

    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "reader", "task": "inspect", "mode": "readonly"}],
            "max_parallel": 1,
        }
    )

    manifest = list_worktree_agents(repo)[0]
    worktree = repo / manifest["worktree_rel"]
    assert not (worktree / "blocked.txt").exists()
    assert manifest["changed_files"] == 0


class _ConcurrentClient(FakeModelClient):
    def __init__(self, activity):
        super().__init__(["done"])
        self.activity = activity

    def complete(self, **kwargs):
        with self.activity["lock"]:
            self.activity["active"] += 1
            self.activity["maximum"] = max(
                self.activity["maximum"], self.activity["active"]
            )
        time.sleep(0.08)
        try:
            return super().complete(**kwargs)
        finally:
            with self.activity["lock"]:
                self.activity["active"] -= 1


def test_batch_honors_max_parallel_with_distinct_clients(tmp_path):
    repo = _repo(tmp_path)
    activity = {"lock": threading.Lock(), "active": 0, "maximum": 0}
    agent = _agent(repo, [_ConcurrentClient(activity) for _ in range(3)])

    agent.spawn_worktree_agents(
        {
            "tasks": [
                {"name": name, "task": "inspect", "mode": "readonly"}
                for name in ("one", "two", "three")
            ],
            "max_parallel": 2,
        }
    )

    assert activity["maximum"] == 2
    assert len(list_worktree_agents(repo)) == 3


def test_reused_client_fails_before_tasks_and_removes_worktrees(tmp_path):
    repo = _repo(tmp_path)
    client = FakeModelClient(["unused"])
    agent = _agent(repo, [client, client])

    with pytest.raises(ValueError, match="reused a client"):
        agent.spawn_worktree_agents(
            {
                "tasks": [
                    {"name": "one", "task": "inspect"},
                    {"name": "two", "task": "inspect"},
                ],
                "max_parallel": 2,
            }
        )

    worktrees = _git(repo, "worktree", "list", "--porcelain").stdout
    branches = _git(repo, "branch", "--list", "codex/pony-agent-*").stdout
    assert worktrees.count("worktree ") == 1
    assert not branches.strip()
    assert not list_worktree_agents(repo)


def test_snapshot_failure_leaves_a_terminal_recoverable_manifest(
    tmp_path,
    monkeypatch,
):
    repo = _repo(tmp_path)
    agent = _agent(repo, [FakeModelClient(["done"])])
    monkeypatch.setattr(
        "pony.runtime.worktree_agents._worktree_snapshot",
        lambda _git, _worktree: (_ for _ in ()).throw(OSError("snapshot unavailable")),
    )

    result = agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "reader", "task": "inspect"}],
            "max_parallel": 1,
        }
    )

    manifest = list_worktree_agents(repo)[0]
    assert manifest["status"] == "failed"
    assert manifest["diff_status"] == "unknown"
    assert manifest["error"] == "worktree finalization failed: snapshot unavailable"
    assert "reader" in result and "failed" in result


def test_explicit_merge_commits_then_cleanup_removes_review_branch(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(
        repo,
        [
            FakeModelClient(
                [
                    {
                        "name": "write_file",
                        "args": {"path": "merged.txt", "content": "merged\n"},
                    },
                    "ready",
                ]
            )
        ],
    )
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "merger", "task": "write file", "mode": "write"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]

    merged = merge_worktree_agent(repo, manifest["id"], _git_executable(agent))

    assert merged["status"] == "merged"
    assert (repo / "merged.txt").read_text(encoding="utf-8") == "merged\n"
    assert "agent(merger): isolated worktree task" in _git(
        repo, "log", "--format=%s", "--all"
    ).stdout
    cleaned = cleanup_worktree_agent(repo, manifest["id"], _git_executable(agent))
    assert cleaned["status"] == "cleaned"
    assert not (repo / manifest["worktree_rel"]).exists()
    assert not _git(repo, "branch", "--list", manifest["branch"]).stdout.strip()


def test_cleanup_recovers_after_branch_and_worktree_were_already_removed(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(repo, [FakeModelClient(["done"])])
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "reader", "task": "inspect"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]
    merge_worktree_agent(repo, manifest["id"], _git_executable(agent))
    worktree = repo / manifest["worktree_rel"]
    _git(repo, "worktree", "remove", "--force", str(worktree))
    _git(repo, "branch", "-D", manifest["branch"])

    cleaned = cleanup_worktree_agent(repo, manifest["id"], _git_executable(agent))

    assert cleaned["status"] == "cleaned"


def test_cleanup_discard_removes_a_terminal_branch_that_was_not_merged(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(
        repo,
        [
            FakeModelClient(
                [
                    {
                        "name": "write_file",
                        "args": {"path": "discard.txt", "content": "discard\n"},
                    },
                    "done",
                ]
            )
        ],
    )
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "writer", "task": "write", "mode": "write"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]

    with pytest.raises(ValueError, match="has not been merged"):
        cleanup_worktree_agent(repo, manifest["id"], _git_executable(agent))

    cleaned = cleanup_worktree_agent(
        repo,
        manifest["id"],
        _git_executable(agent),
        discard=True,
    )

    assert cleaned["status"] == "cleaned"
    assert not (repo / manifest["worktree_rel"]).exists()


def test_merge_rejects_any_post_completion_edit_without_staging_it(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(repo, [FakeModelClient(["done"])])
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "reader", "task": "inspect"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]
    worktree = repo / manifest["worktree_rel"]
    (worktree / "late.txt").write_text("late\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changes after completion"):
        merge_worktree_agent(repo, manifest["id"], _git_executable(agent))

    assert not (repo / "late.txt").exists()


def test_test_status_does_not_claim_prior_verification_for_later_edit():
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "run_shell",
                    "input": {"command": "pytest -q"},
                }
            ],
        },
        {"role": "user", "content": "ok", "_pony_meta": {"tool_status": "ok"}},
        {
            "role": "user",
            "content": "edited",
            "_pony_meta": {"workspace_changed": True},
        },
    ]

    from pony.runtime.worktree_agents import _test_status

    assert _test_status(messages) == "not_run"


def test_merge_conflict_is_rejected_before_parent_changes(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(
        repo,
        [
            FakeModelClient(
                [
                    {
                        "name": "write_file",
                        "args": {"path": "README.md", "content": "child\n"},
                    },
                    "done",
                ]
            )
        ],
    )
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "conflict", "task": "edit", "mode": "write"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]
    (repo / "README.md").write_text("parent\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "parent edit",
    )
    parent_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(ValueError, match="merge has conflicts"):
        merge_worktree_agent(repo, manifest["id"], _git_executable(agent))

    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == parent_head
    assert (repo / "README.md").read_text(encoding="utf-8") == "parent\n"
    assert _git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode


def test_merge_rejects_sensitive_path_already_committed_by_child(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(repo, [FakeModelClient(["done"])])
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "sensitive", "task": "inspect"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]
    worktree = repo / manifest["worktree_rel"]
    (worktree / ".env").write_text("SECRET=value\n", encoding="utf-8")
    _git(worktree, "add", "-f", ".env")
    _git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "unsafe",
    )

    with pytest.raises(ValueError, match="branch changed after completion"):
        merge_worktree_agent(repo, manifest["id"], _git_executable(agent))

    assert not (repo / ".env").exists()


def test_merge_rejects_symlink_already_committed_by_child(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(repo, [FakeModelClient(["done"])])
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "symlink", "task": "inspect"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]
    worktree = repo / manifest["worktree_rel"]
    (worktree / "linked-readme").symlink_to("README.md")
    _git(worktree, "add", "linked-readme")
    _git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "unsafe",
    )

    with pytest.raises(ValueError, match="branch changed after completion"):
        merge_worktree_agent(repo, manifest["id"], _git_executable(agent))

    assert not (repo / "linked-readme").exists()


def test_merge_rejects_uncommitted_hardlinks(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(repo, [FakeModelClient(["done"])])
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "hardlink", "task": "inspect"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]
    worktree = repo / manifest["worktree_rel"]
    target = worktree / "hardlink-target"
    target.write_text("linked\n", encoding="utf-8")
    (worktree / "hardlink").hardlink_to(target)

    with pytest.raises(ValueError, match="changes after completion"):
        merge_worktree_agent(repo, manifest["id"], _git_executable(agent))

    assert not (repo / "hardlink").exists()


def test_special_file_cannot_enter_parent_merge(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(repo, [FakeModelClient(["done"])])
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "fifo", "task": "inspect"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]
    worktree = repo / manifest["worktree_rel"]
    os.mkfifo(worktree / "fifo")

    merged = merge_worktree_agent(repo, manifest["id"], _git_executable(agent))

    assert merged["status"] == "merged"
    assert not (repo / "fifo").exists()


def test_merge_rejects_product_state_already_committed_by_child(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(repo, [FakeModelClient(["done"])])
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "product-state", "task": "inspect"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]
    worktree = repo / manifest["worktree_rel"]
    product_state = worktree / ".pony" / "unexpected.txt"
    product_state.write_text("state\n", encoding="utf-8")
    _git(worktree, "add", "-f", ".pony/unexpected.txt")
    _git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "unsafe",
    )

    with pytest.raises(ValueError, match="branch changed after completion"):
        merge_worktree_agent(repo, manifest["id"], _git_executable(agent))

    assert not (repo / ".pony" / "unexpected.txt").exists()


def test_dirty_parent_is_rejected_before_any_worktree_is_created(tmp_path):
    repo = _repo(tmp_path)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    agent = _agent(repo, [FakeModelClient(["unused"])])

    with pytest.raises(ValueError, match="clean parent worktree"):
        agent.spawn_worktree_agents(
            {
                "tasks": [{"name": "reader", "task": "inspect"}],
                "max_parallel": 1,
            }
        )

    assert not list_worktree_agents(repo)


def test_detached_parent_is_rejected_before_any_worktree_is_created(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "--detach", "HEAD")
    agent = _agent(repo, [FakeModelClient(["unused"])])

    with pytest.raises(ValueError, match="checked-out parent branch"):
        agent.spawn_worktree_agents(
            {
                "tasks": [{"name": "reader", "task": "inspect"}],
                "max_parallel": 1,
            }
        )

    assert not list_worktree_agents(repo)


def test_agents_list_is_a_model_free_cli_command(tmp_path, capsys):
    repo = _repo(tmp_path)

    assert main(["--cwd", str(repo), "agents", "list"]) == 0
    assert "no worktree agents" in capsys.readouterr().out


def test_agents_merge_requires_project_trust_before_mutating(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    agent = _agent(repo, [FakeModelClient(["done"])])
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "reader", "task": "inspect"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]
    monkeypatch.setattr(
        "pony.cli.app.trusted_project_root",
        lambda _args: (_ for _ in ()).throw(
            CliError(
                code="project_untrusted",
                message="Project is not trusted",
                exit_code=4,
            )
        ),
    )

    assert main(["--cwd", str(repo), "agents", "merge", manifest["id"]]) == 4
    assert "Project is not trusted" in capsys.readouterr().err


def test_agents_list_maps_invalid_manifest_to_stable_cli_error(tmp_path, capsys):
    repo = _repo(tmp_path)
    agent = _agent(repo, [FakeModelClient(["done"])])
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "reader", "task": "inspect"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]
    manifest_path = (
        repo / ".pony" / "worktree-agents" / manifest["id"] / "manifest.json"
    )
    manifest_path.write_text("{}\n", encoding="utf-8")

    assert main(["--cwd", str(repo), "agents", "list"]) == 1
    assert capsys.readouterr().err.strip() == "invalid worktree agent manifest"


def test_agents_cli_performs_explicit_merge_and_cleanup(tmp_path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    agent = _agent(
        repo,
        [
            FakeModelClient(
                [
                    {
                        "name": "write_file",
                        "args": {"path": "cli.txt", "content": "cli\n"},
                    },
                    "done",
                ]
            )
        ],
    )
    agent.spawn_worktree_agents(
        {
            "tasks": [{"name": "cli", "task": "write file", "mode": "write"}],
            "max_parallel": 1,
        }
    )
    manifest = list_worktree_agents(repo)[0]
    trust_store = SimpleNamespace(is_trusted=lambda _root: True)
    monkeypatch.setattr(
        "pony.cli.app.trusted_project_root",
        lambda _args: (repo, (repo.stat().st_dev, repo.stat().st_ino), trust_store),
    )

    assert main(["--cwd", str(repo), "agents", "merge", manifest["id"]]) == 0
    assert "status: merged" in capsys.readouterr().out
    assert (repo / "cli.txt").read_text(encoding="utf-8") == "cli\n"

    assert main(["--cwd", str(repo), "agents", "cleanup", manifest["id"]]) == 0
    assert "status: cleaned" in capsys.readouterr().out
