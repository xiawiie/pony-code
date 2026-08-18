$ErrorActionPreference = "Stop"

if ($args.Count -ne 0) {
    [Console]::Error.WriteLine("usage: scripts/windows/verify_focused_runtime.ps1")
    exit 2
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$uv = if ($env:PONY_CI_UV) { $env:PONY_CI_UV } else { "uv" }
Set-Location -LiteralPath $repoRoot

& $uv run --frozen pytest -q -ra --durations=20 `
    -p scripts.windows.pytest_skip_policy `
    tests/e2e/test_full_turn_roundtrip.py `
    tests/e2e/test_native_provider_roundtrip.py `
    tests/test_file_lock.py `
    tests/test_input_queue.py `
    tests/test_private_paths.py `
    tests/test_project_env_security.py `
    tests/test_safe_subprocess.py `
    tests/test_workspace_io_security.py
exit $LASTEXITCODE
