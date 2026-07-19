#!/usr/bin/env sh
set -eu

if [ "$#" -eq 0 ]; then
  release_dist=0
elif [ "$#" -eq 1 ] && [ "$1" = "--release-dist" ]; then
  release_dist=1
else
  echo "usage: $0 [--release-dist]" >&2
  exit 2
fi

start_head=$(git rev-parse HEAD)
if [ "$release_dist" -eq 1 ] && { [ -e dist ] || [ -L dist ]; }; then
  echo "release dist already exists" >&2
  exit 1
fi
if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
  echo "check requires a clean worktree" >&2
  exit 1
fi
echo "checking clean exact HEAD $start_head"

tmp_dir=$(mktemp -d ".pony-check.XXXXXX")
cleanup() {
  status=$?
  trap - 0
  rm -rf "$tmp_dir" || [ "$status" -ne 0 ]
  exit "$status"
}
trap cleanup 0
trap 'exit 129' 1
trap 'exit 130' 2
trap 'exit 143' 15

dist_dir="$tmp_dir/dist"
UV_OFFLINE=1
export UV_OFFLINE

uv lock --check
uv run --frozen ruff check .
uv run --frozen pytest -q tests benchmarks/live_e2e/tests/test_assertions.py
uv run --frozen python scripts/evaluation/evaluate.py \
  --suite core-functional \
  --output-dir "$tmp_dir/eval"
uv build --offline --clear --out-dir "$dist_dir"
uv run --frozen python scripts/release/verify_distribution.py \
  --dist-dir "$dist_dir" \
  --install-smoke \
  --offline-bundle-smoke

if [ "$(git rev-parse HEAD)" != "$start_head" ] || \
  [ -n "$(git status --porcelain --untracked-files=all)" ]; then
  echo "check did not finish on its clean starting HEAD" >&2
  exit 1
fi
if [ "$release_dist" -eq 1 ]; then
  if [ -e dist ] || [ -L dist ]; then
    echo "release dist appeared while checks were running" >&2
    exit 1
  fi
  uv run --frozen python -c \
    'import os, sys; os.rename(sys.argv[1], sys.argv[2])' \
    "$dist_dir" dist
fi
echo "verified clean exact HEAD $start_head"
