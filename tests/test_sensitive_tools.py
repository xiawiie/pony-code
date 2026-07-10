from pathlib import Path
from unittest.mock import Mock

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.safe_subprocess import build_trusted_executables
from pico.tool_executor import ToolExecutionResult


SECRET = "github_pat_A123456789012345678901234567890"


def build_agent(tmp_path, *, executables=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(
        tmp_path,
        executables={} if executables is None else executables,
    )
    return Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


@pytest.mark.parametrize(
    ("name", "arguments"),
    (
        ("read_file", {"path": ".env"}),
        ("search", {"pattern": "secret", "path": ".ssh"}),
        ("list_files", {"path": ".ssh"}),
        ("write_file", {"path": "CLIENT.PEM", "content": "x"}),
        (
            "patch_file",
            {
                "path": ".pico/sessions/manual.json",
                "old_text": "x",
                "new_text": "y",
            },
        ),
    ),
)
def test_sensitive_direct_paths_are_rejected_before_runner(
    tmp_path,
    name,
    arguments,
):
    agent = build_agent(tmp_path)
    (tmp_path / ".env").write_text("PICO_API_KEY=opaque\n", encoding="utf-8")
    (tmp_path / ".ssh").mkdir()
    session_target = tmp_path / ".pico" / "sessions" / "manual.json"
    session_target.write_text("x", encoding="utf-8")
    runner = Mock(return_value="must not run")
    agent.tools[name]["run"] = runner

    result = agent.execute_tool(name, arguments)

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "sensitive_path_block"
    assert result.metadata["security_event_type"] == "sensitive_access_block"
    runner.assert_not_called()
    assert list(agent.checkpoint_store.blobs_dir.rglob("*")) == []


@pytest.mark.parametrize(
    "raw_path",
    ("sub/../.ENV",),
)
def test_raw_lexical_aliases_are_rejected_before_resolution(tmp_path, raw_path):
    agent = build_agent(tmp_path)
    (tmp_path / ".ENV").write_text("secret\n", encoding="utf-8")
    (tmp_path / "alias.txt").symlink_to(tmp_path / "README.md")
    runner = Mock(return_value="must not run")
    agent.tools["read_file"]["run"] = runner

    result = agent.execute_tool("read_file", {"path": raw_path})

    assert result.metadata["tool_error_code"] == "sensitive_path_block"
    runner.assert_not_called()
    assert list(agent.checkpoint_store.blobs_dir.rglob("*")) == []


def test_benign_symlink_is_rejected_before_resolution_with_compatible_error(tmp_path):
    agent = build_agent(tmp_path)
    (tmp_path / "alias.txt").symlink_to(tmp_path / "README.md")
    runner = Mock(return_value="must not run")
    agent.tools["read_file"]["run"] = runner

    result = agent.execute_tool("read_file", {"path": "alias.txt"})

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "invalid_arguments"
    assert "path escapes workspace" in result.content
    runner.assert_not_called()


def test_absolute_in_root_sensitive_path_is_rejected(tmp_path):
    target = tmp_path / ".env"
    target.write_text("secret\n", encoding="utf-8")
    agent = build_agent(tmp_path)
    runner = Mock(return_value="must not run")
    agent.tools["read_file"]["run"] = runner

    result = agent.execute_tool("read_file", {"path": str(target)})

    assert result.metadata["tool_error_code"] == "sensitive_path_block"
    runner.assert_not_called()


def test_sensitive_descendant_is_rejected_before_direct_read_or_search(tmp_path):
    agent = build_agent(tmp_path)
    sensitive_dir = tmp_path / ".env"
    sensitive_dir.mkdir()
    child = sensitive_dir / "child.txt"
    child.write_text("descendant sentinel\n", encoding="utf-8")
    read_runner = Mock(return_value="must not run")
    agent.tools["read_file"]["run"] = read_runner

    blocked = agent.execute_tool("read_file", {"path": ".env/child.txt"})
    searched = agent.execute_tool(
        "search",
        {"pattern": "descendant sentinel", "path": "."},
    )

    assert blocked.metadata["tool_error_code"] == "sensitive_path_block"
    read_runner.assert_not_called()
    assert ".env/child.txt" not in searched.content


def test_secret_content_write_is_rejected_but_security_prose_is_allowed(tmp_path):
    secret = "opaque-project-value-123456789"
    agent = build_agent(
        tmp_path,
        redaction_env={"PICO_CUSTOM_SECRET": secret},
        secret_env_names=("PICO_CUSTOM_SECRET",),
    )

    blocked = agent.execute_tool(
        "write_file",
        {"path": "notes.txt", "content": secret},
    )
    allowed = agent.execute_tool(
        "write_file",
        {"path": "policy.txt", "content": "password policy"},
    )

    assert blocked.metadata["tool_status"] == "rejected"
    assert blocked.metadata["tool_error_code"] == "sensitive_content_block"
    assert blocked.metadata["security_event_type"] == "sensitive_access_block"
    assert not (tmp_path / "notes.txt").exists()
    assert allowed.metadata["tool_status"] == "ok"
    assert (tmp_path / "policy.txt").read_text(encoding="utf-8") == "password policy"


def test_patch_scans_complete_would_be_content_before_runner(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("safe body\n", encoding="utf-8")
    agent = build_agent(tmp_path)
    runner = Mock(return_value="must not run")
    agent.tools["patch_file"]["run"] = runner

    result = agent.execute_tool(
        "patch_file",
        {"path": "notes.txt", "old_text": "safe", "new_text": SECRET},
    )

    assert result.metadata["tool_error_code"] == "sensitive_content_block"
    assert target.read_text(encoding="utf-8") == "safe body\n"
    runner.assert_not_called()


def test_risky_write_revalidates_after_approval_swaps_target_to_symlink(
    tmp_path,
):
    agent = build_agent(tmp_path)
    sensitive = tmp_path / ".env"
    sensitive.write_text("untouched\n", encoding="utf-8")
    target = tmp_path / "safe.txt"
    approvals = []

    def approve(name, args):
        approvals.append((name, args))
        target.symlink_to(sensitive)
        return True

    agent.approve = approve
    runner = Mock(return_value="must not run")
    agent.tools["write_file"]["run"] = runner

    result = agent.execute_tool(
        "write_file",
        {"path": "safe.txt", "content": "safe body"},
    )

    assert len(approvals) == 1
    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "invalid_arguments"
    assert sensitive.read_text(encoding="utf-8") == "untouched\n"
    runner.assert_not_called()
    assert list(agent.checkpoint_store.blobs_dir.rglob("*")) == []
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_risky_patch_revalidates_content_changed_during_approval(tmp_path):
    secret = "github_pat_A123456789012345678901234567890"
    target = tmp_path / "safe.txt"
    target.write_text("old safe\n", encoding="utf-8")
    agent = build_agent(tmp_path)
    approvals = []

    def approve(name, args):
        approvals.append((name, args))
        target.write_text("old " + secret + "\n", encoding="utf-8")
        return True

    agent.approve = approve
    runner = Mock(return_value="must not run")
    agent.tools["patch_file"]["run"] = runner

    result = agent.execute_tool(
        "patch_file",
        {"path": "safe.txt", "old_text": "old", "new_text": "new"},
    )

    assert len(approvals) == 1
    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "sensitive_content_block"
    assert target.read_text(encoding="utf-8") == "old " + secret + "\n"
    runner.assert_not_called()
    assert list(agent.checkpoint_store.blobs_dir.rglob("*")) == []
    assert agent.checkpoint_store.list_tool_change_records() == []


def test_memory_save_secret_is_rejected_before_runner(tmp_path):
    secret = "opaque-memory-value-123456789"
    agent = build_agent(
        tmp_path,
        redaction_env={"CUSTOM_CREDENTIAL": secret},
        secret_env_names=("CUSTOM_CREDENTIAL",),
    )
    runner = Mock(return_value="must not run")
    agent.tools["memory_save"]["run"] = runner

    result = agent.execute_tool("memory_save", {"note": secret})

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "sensitive_content_block"
    assert result.metadata["security_event_type"] == "sensitive_access_block"
    runner.assert_not_called()


def test_list_files_names_sensitive_entry_without_child_metadata_access(
    tmp_path,
    monkeypatch,
):
    sensitive = tmp_path / ".env"
    sensitive.write_text("PICO_API_KEY=opaque-value", encoding="utf-8")
    agent = build_agent(tmp_path)
    real_stat = Path.stat
    real_lstat = Path.lstat

    def guarded_stat(self, *args, **kwargs):
        if self == sensitive:
            raise AssertionError("sensitive child stat")
        return real_stat(self, *args, **kwargs)

    def guarded_lstat(self, *args, **kwargs):
        if self == sensitive:
            raise AssertionError("sensitive child lstat")
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(Path, "lstat", guarded_lstat)

    result = agent.execute_tool("list_files", {"path": "."})

    sensitive_line = next(
        line for line in result.content.splitlines() if line.startswith(".env")
    )
    assert sensitive_line == ".env [sensitive]"
    assert "opaque-value" not in result.content


@pytest.mark.parametrize("use_rg", (False, True), ids=("python", "rg"))
def test_directory_search_excludes_sensitive_paths_without_path_rescan(
    tmp_path,
    monkeypatch,
    use_rg,
):
    sentinel = "shared-search-sentinel"
    (tmp_path / ".env").write_text(sentinel + "\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text(sentinel + "\n", encoding="utf-8")
    (tmp_path / "credentials.json").write_text(sentinel + "\n", encoding="utf-8")
    (tmp_path / "source.py").write_text(sentinel + "\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(sentinel + "\n", encoding="utf-8")
    hidden_memory = tmp_path / ".pico" / "memory" / "cache.md"
    hidden_memory.parent.mkdir(parents=True)
    hidden_memory.write_text(sentinel + "\n", encoding="utf-8")
    executables = {}
    if use_rg:
        rg = build_trusted_executables(tmp_path, names=("rg",)).get("rg")
        if not rg:
            pytest.skip("trusted rg unavailable")
        executables["rg"] = rg
    monkeypatch.setattr(
        "shutil.which",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("runtime PATH rescan")
        ),
    )
    agent = build_agent(tmp_path, executables=executables)

    result = agent.execute_tool(
        "search",
        {"pattern": sentinel, "path": "."},
    )

    assert f"source.py:1:{sentinel}" in result.content
    assert f".env.example:1:{sentinel}" in result.content
    assert not any(
        line.startswith(".env:")
        for line in result.content.splitlines()
    )
    assert "credentials.json" not in result.content
    assert ".git/" not in result.content
    assert ".pico/" not in result.content


@pytest.mark.parametrize(
    ("contents", "pattern", "expected", "unexpected"),
    (
        (
            "PICO_TOKEN=one\nPICOxTOKEN=two\n",
            r"^PICO_TOKEN=",
            ".env.example:1:PICO_TOKEN=one",
            ".env.example:2:PICOxTOKEN=two",
        ),
        (
            "OTHER=Pico\nLOWER=pico\n",
            "Pico",
            ".env.example:1:OTHER=Pico",
            ".env.example:2:LOWER=pico",
        ),
    ),
    ids=("regex", "smart-case"),
)
def test_rg_search_preserves_rg_semantics_for_allowed_env_templates(
    tmp_path,
    contents,
    pattern,
    expected,
    unexpected,
):
    rg = build_trusted_executables(tmp_path, names=("rg",)).get("rg")
    if not rg:
        pytest.skip("trusted rg unavailable")
    (tmp_path / ".env.example").write_text(contents, encoding="utf-8")
    agent = build_agent(tmp_path, executables={"rg": rg})

    result = agent.execute_tool(
        "search",
        {"pattern": pattern, "path": "."},
    )

    assert expected in result.content
    assert unexpected not in result.content


def test_execute_tool_returns_a_redacted_copy_and_preserves_metadata_types(
    tmp_path,
    monkeypatch,
):
    agent = build_agent(tmp_path)
    original = ToolExecutionResult(
        content=SECRET,
        metadata={
            "tool_status": "ok",
            "details": {"stdout": SECRET},
            "ordered": (SECRET, "safe"),
        },
    )
    monkeypatch.setattr(agent.tool_executor, "execute", lambda name, args: original)

    result = agent.execute_tool("read_file", {"path": "README.md"})

    assert result is not original
    assert SECRET not in result.content
    assert SECRET not in str(result.metadata)
    assert isinstance(result.metadata["details"], dict)
    assert isinstance(result.metadata["ordered"], tuple)
    assert original.content == SECRET
    assert original.metadata["details"]["stdout"] == SECRET
    assert agent._last_tool_result_metadata == result.metadata


def test_runner_result_is_redacted_before_clip_can_split_known_secret(tmp_path):
    secret = "opaque-boundary-value-" + "z" * 40
    agent = build_agent(
        tmp_path,
        redaction_env={"CUSTOM_CREDENTIAL": secret},
        secret_env_names=("CUSTOM_CREDENTIAL",),
    )
    agent.tools["read_file"]["run"] = Mock(
        return_value="x" * 3990 + secret + "tail"
    )

    result = agent.execute_tool("read_file", {"path": "README.md"})

    assert secret not in result.content
    assert secret[:20] not in result.content
    assert "<redacted>" in result.content
