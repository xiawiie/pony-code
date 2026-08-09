#!/usr/bin/env python3
"""Run Pony's decision-focused compaction and Provider latency evaluation."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.evaluation.efficiency_evaluation import (  # noqa: E402
    run_efficiency_evaluation,
)
from pony.security.private_files import (  # noqa: E402
    private_directory_identity,
    write_private_bytes_atomic,
)


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository whose canonical .env selects the single Provider target.",
    )
    parser.add_argument(
        "--compaction-repetitions",
        type=int,
        default=3,
        help="Paired trials per compaction/resume scenario.",
    )
    parser.add_argument(
        "--latency-repetitions",
        type=int,
        default=3,
        help="Trials per fixed latency workload.",
    )
    parser.add_argument("--output-json", required=True)
    return parser


def _write_private_json(path, payload):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    write_private_bytes_atomic(
        path,
        data,
        trusted_root=path.parent,
        trusted_root_identity=private_directory_identity(path.parent),
    )


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if args.compaction_repetitions < 1 or args.latency_repetitions < 1:
        raise SystemExit("evaluation repetitions must be positive")
    payload = run_efficiency_evaluation(
        repo_root=args.repo_root,
        compaction_repetitions=args.compaction_repetitions,
        latency_repetitions=args.latency_repetitions,
    )
    _write_private_json(args.output_json, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
