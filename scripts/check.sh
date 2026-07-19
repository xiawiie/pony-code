#!/usr/bin/env sh
set -eu

start_head=$(git rev-parse HEAD)
if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
  echo "check requires a clean worktree" >&2
  exit 1
fi
echo "checking clean exact HEAD $start_head"

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/pony-check.XXXXXX")
trap 'rm -rf "$tmp_dir"' 0 1 2 15

uv lock --check
uv run --frozen ruff check .
uv run --frozen pytest -q tests benchmarks/live_e2e/tests/test_assertions.py
uv run --frozen python scripts/evaluation/evaluate.py --suite core-functional
uv build --offline --out-dir "$tmp_dir/dist"
uv run --frozen python scripts/release/verify_distribution.py \
  --dist-dir "$tmp_dir/dist" \
  --install-smoke \
  --offline-bundle-smoke

if [ "$(git rev-parse HEAD)" != "$start_head" ] || \
  [ -n "$(git status --porcelain --untracked-files=all)" ]; then
  echo "check did not finish on its clean starting HEAD" >&2
  exit 1
fi
echo "verified clean exact HEAD $start_head"
