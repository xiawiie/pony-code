$ErrorActionPreference = "Stop"
$uv = $env:PONY_CI_UV
$dist = Join-Path $env:RUNNER_TEMP "pony-dist"
$evaluation = Join-Path $env:RUNNER_TEMP "pony-windows-eval"

& git.exe config --global --add safe.directory $env:GITHUB_WORKSPACE
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$trustedPythonProbe = @'
from pathlib import Path
import os
import shutil
from pony.tools import subprocess as safe_subprocess

candidates = [
    Path(value)
    for value in os.environ.get("PATH", "").split(os.pathsep)
    if Path(value).name.casefold() == "python"
    and Path(value).parent.name.startswith("pony-ci-tools-")
]
candidate = candidates[0] if len(candidates) == 1 else None
print("trusted_python_candidate_count", len(candidates))
print("trusted_python_pathext_present", bool(os.environ.get("PATHEXT")))
print("trusted_python_exe_exists", bool(candidate and (candidate / "python.exe").is_file()))
print("trusted_python_which", bool(candidate and shutil.which("python", path=str(candidate))))
safe_dirs = safe_subprocess._safe_path_dirs(Path.cwd(), os.environ)
print("trusted_python_safe_dir", bool(candidate and str(candidate) in safe_dirs))
print(
    "trusted_python_discovered",
    "python" in safe_subprocess.build_trusted_executables(Path.cwd(), names=("python",)),
)
'@
$trustedPythonProbe | & $uv run --frozen python -
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

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
