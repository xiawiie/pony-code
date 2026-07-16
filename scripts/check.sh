#!/usr/bin/env sh
set -eu

uv lock --check
uv run --frozen ruff check .
uv run --frozen pytest -q
uv run --frozen python scripts/evaluation/evaluate.py --suite core-functional
uv run --frozen pytest benchmarks/live_e2e/tests/test_assertions.py -q
uv build --clear
uv run --frozen python scripts/release/verify_distribution.py \
  --install-smoke \
  --offline-bundle-smoke
