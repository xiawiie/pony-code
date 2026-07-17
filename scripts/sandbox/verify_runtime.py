#!/usr/bin/env python3
"""Verify Pony's packaged local Docker Sandbox runtime contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def _pony(action: str) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, "-m", "pony", "--format", "json", "sandbox", action],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"sandbox {action} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"sandbox {action} returned invalid data")
    return result.returncode, payload


def verify(*, require_ready: bool) -> dict:
    status_code, status = _pony("status")
    if status_code != 0 or status.get("ok") is not True:
        raise RuntimeError("sandbox status failed")
    status_data = status.get("data")
    if (
        not isinstance(status_data, dict)
        or status_data.get("network_performed") is not False
        or status_data.get("mutation_performed") is not False
        or status_data.get("runtime_authorization", {}).get("kind") != "local"
    ):
        raise RuntimeError("sandbox status contract mismatch")
    prepare_code, prepared = _pony("prepare")
    ready = prepare_code == 0 and prepared.get("ok") is True
    if prepare_code not in {0, 3}:
        raise RuntimeError("sandbox prepare returned an unexpected exit code")
    if require_ready and not ready:
        error = prepared.get("error", {})
        raise RuntimeError(f"sandbox is not ready: {error.get('code', 'unknown')}")
    return {
        "record_type": "pony_sandbox_runtime_verification",
        "format_version": 1,
        "status": "ready" if ready else "not_ready",
        "reason_code": "ready" if ready else prepared.get("error", {}).get("code", "unknown"),
        "runtime_authorization": "local",
        "network_performed": False,
        "mutation_performed": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-ready", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = verify(require_ready=args.require_ready)
    except RuntimeError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["status"] == "ready" or not args.require_ready else 3


if __name__ == "__main__":
    raise SystemExit(main())
