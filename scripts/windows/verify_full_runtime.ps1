$ErrorActionPreference = "Stop"
$uv = $env:PONY_CI_UV
$dist = Join-Path $env:RUNNER_TEMP "pony-dist"
$evaluation = Join-Path $env:RUNNER_TEMP "pony-windows-eval"

& $uv run --frozen pytest -x -vv tests benchmarks/live_e2e/tests/test_assertions.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $uv run --frozen python scripts/evaluation/evaluate.py `
    --suite core-functional `
    --output-dir $evaluation
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $uv build --offline --clear --no-create-gitignore --out-dir $dist
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $uv run --frozen python scripts/release/verify_distribution.py `
    --dist-dir $dist `
    --install-smoke `
    --offline-bundle-smoke
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $uv run pony --help
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $uv run pony status
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& cmd.exe /d /c ".venv\Scripts\pony.exe --help"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $uv run python -c "import prompt_toolkit; import pony.tui.app"
exit $LASTEXITCODE
