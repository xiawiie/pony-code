#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.evaluation.provider_benchmark import (  # noqa: E402
    DEFAULT_PROVIDER_EXPERIMENT_MAX_OUTPUT_TOKENS,
    PROVIDER_EXPERIMENT_FORMAT_VERSION,
    run_provider_experiments,
)
from benchmarks.evaluation.metrics_common import _validate_record_header  # noqa: E402


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Run GPT, Claude, and DeepSeek provider experiments for pico benchmark tasks.")
    parser.add_argument("--benchmark-path", default="benchmarks/coding_tasks.json", help="Path to benchmark task JSON.")
    parser.add_argument("--workspace-root", default="artifacts/provider-workspaces", help="Workspace root for provider experiment copies.")
    parser.add_argument("--artifact-root", default="artifacts/provider-artifacts", help="Directory to store provider benchmark artifacts.")
    parser.add_argument("--output-json", required=True, help="Path to output provider experiment JSON.")
    parser.add_argument(
        "--provider",
        choices=("all", "gpt", "claude", "deepseek"),
        default="all",
        help="Provider benchmark target. Use 'all' to run GPT, Claude, and DeepSeek.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=DEFAULT_PROVIDER_EXPERIMENT_MAX_OUTPUT_TOKENS,
        help="Max output tokens per provider run.",
    )
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    payload = run_provider_experiments(
        benchmark_path=args.benchmark_path,
        workspace_root=args.workspace_root,
        artifact_root=args.artifact_root,
        max_output_tokens=args.max_output_tokens,
        providers=args.provider,
    )
    _validate_record_header(
        payload,
        "provider_experiment_result",
        PROVIDER_EXPERIMENT_FORMAT_VERSION,
    )
    output = Path(args.output_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
