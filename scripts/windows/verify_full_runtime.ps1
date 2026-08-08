$ErrorActionPreference = "Stop"

if ($args.Count -ne 0) {
    [Console]::Error.WriteLine("usage: scripts/windows/verify_full_runtime.ps1")
    exit 2
}

function Invoke-CheckedNative {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(Mandatory = $true)]
        [string[]]$CommandArguments
    )

    & $Command @CommandArguments
    $status = $LASTEXITCODE
    if ($status -ne 0) {
        $script:ExitCode = $status
        throw "command failed with exit code ${status}: $Command $($CommandArguments -join ' ')"
    }
}

$originalLocation = Get-Location
$temporaryRoot = $null
$script:ExitCode = 0

try {
    $repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    Set-Location -LiteralPath $repoRoot

    $startHeadOutput = @(& git rev-parse HEAD)
    if ($LASTEXITCODE -ne 0) {
        $script:ExitCode = $LASTEXITCODE
        throw "failed to resolve the starting Git HEAD"
    }
    $startHead = ($startHeadOutput -join "`n").Trim()
    $startStatus = @(& git status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) {
        $script:ExitCode = $LASTEXITCODE
        throw "failed to inspect the starting worktree"
    }
    if ($startStatus.Count -ne 0) {
        $script:ExitCode = 1
        throw "check requires a clean worktree"
    }
    Write-Output "checking clean exact HEAD $startHead"

    $temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) (
        "pony-check-" + [Guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    $distDir = Join-Path $temporaryRoot "dist"
    $evaluationDir = Join-Path $temporaryRoot "eval"
    $uv = if ($env:PONY_CI_UV) { $env:PONY_CI_UV } else { "uv" }
    $env:UV_OFFLINE = "1"

    Invoke-CheckedNative $uv @("lock", "--check")
    Invoke-CheckedNative $uv @("run", "--frozen", "ruff", "check", ".")
    Invoke-CheckedNative $uv @(
        "run", "--frozen", "pytest", "-q",
        "tests", "benchmarks/live_e2e/tests/test_assertions.py"
    )
    Invoke-CheckedNative $uv @(
        "run", "--frozen", "python", "scripts/evaluation/evaluate.py",
        "--suite", "core-functional", "--output-dir", $evaluationDir
    )
    Invoke-CheckedNative $uv @(
        "build", "--offline", "--clear", "--no-create-gitignore",
        "--out-dir", $distDir
    )
    Invoke-CheckedNative $uv @(
        "run", "--frozen", "python", "scripts/release/verify_distribution.py",
        "--dist-dir", $distDir, "--install-smoke", "--offline-bundle-smoke"
    )
    Invoke-CheckedNative $uv @("run", "--frozen", "pony", "--help")
    Invoke-CheckedNative $uv @("run", "--frozen", "pony", "status")

    $escapedUv = $uv.Replace('"', '""')
    $cmdLine = '"' + $escapedUv + '" run --frozen pony --help'
    Invoke-CheckedNative "cmd.exe" @("/d", "/s", "/c", $cmdLine)
    Invoke-CheckedNative $uv @(
        "run", "--frozen", "python", "-c",
        "import prompt_toolkit; import pony.tui.app"
    )

    $endHeadOutput = @(& git rev-parse HEAD)
    if ($LASTEXITCODE -ne 0) {
        $script:ExitCode = $LASTEXITCODE
        throw "failed to resolve the final Git HEAD"
    }
    $endHead = ($endHeadOutput -join "`n").Trim()
    $endStatus = @(& git status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) {
        $script:ExitCode = $LASTEXITCODE
        throw "failed to inspect the final worktree"
    }
    if ($endHead -ne $startHead -or $endStatus.Count -ne 0) {
        $script:ExitCode = 1
        throw "check did not finish on its clean starting HEAD"
    }
    Write-Output "verified clean exact HEAD $startHead"
}
catch {
    if ($script:ExitCode -eq 0) {
        $script:ExitCode = 1
    }
    [Console]::Error.WriteLine($_.Exception.Message)
}
finally {
    Set-Location $originalLocation
    if ($temporaryRoot -and (Test-Path -LiteralPath $temporaryRoot)) {
        try {
            Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
        }
        catch {
            if ($script:ExitCode -eq 0) {
                $script:ExitCode = 1
            }
            [Console]::Error.WriteLine(
                "failed to remove temporary check directory: $($_.Exception.Message)"
            )
        }
    }
}

exit $script:ExitCode
