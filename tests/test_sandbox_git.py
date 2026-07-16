from pico.tools.shell import ApprovedShellExecution, sandbox_privilege_denial


def test_sandbox_rejects_git_metadata_writes_before_runner(tmp_path):
    execution = ApprovedShellExecution(
        argv=("git", "commit", "-m", "message"),
        exact_command="git commit -m message",
        execution_mode="argv",
        executable="/usr/bin/git",
        timeout=5,
    )
    assert (
        sandbox_privilege_denial(execution, sandbox_mode=True)
        == "sandbox_git_metadata_write_denied"
    )
