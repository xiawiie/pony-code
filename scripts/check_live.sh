#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR/.."
ROOT="${1:-$(pwd)}"
uv run python scripts/live_model_smoke.py "$ROOT"
