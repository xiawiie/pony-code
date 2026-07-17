from pico.tools.subprocess import build_trusted_executables, run_process_group


def test_approved_shell_runner_returns_structured_process_result(tmp_path):
    python = build_trusted_executables(tmp_path, names=("python3",))["python3"]
    result = run_process_group(
        [python, "-c", "print('ok')"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        timeout=5,
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
    assert result.timed_out is False
