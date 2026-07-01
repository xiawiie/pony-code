#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico.metrics import collect_resume_metrics, render_resume_metrics_markdown  # noqa: E402


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Collect pico resume metrics from benchmark and run artifacts.")
    parser.add_argument("--benchmark-artifact", required=True, help="Path to benchmark artifact JSON.")
    parser.add_argument("--runs-root", required=True, help="Path to .pico/runs root.")
    parser.add_argument("--provider-experiments", default=None, help="Optional provider experiments JSON.")
    parser.add_argument("--experiment-mode", choices=("synthetic", "real"), default="synthetic", help="Whether to use deterministic synthetic experiments or real model runs.")
    parser.add_argument("--real-provider", choices=("gpt", "claude", "deepseek"), default="gpt", help="Provider to use for real experiment mode.")
    parser.add_argument("--memory-repetitions", type=int, default=3, help="Repetitions for the small memory experiment.")
    parser.add_argument("--large-memory-repetitions", type=int, default=5, help="Repetitions for the large memory experiment.")
    parser.add_argument("--context-repetitions", type=int, default=5, help="Repetitions for the context stress matrix.")
    parser.add_argument("--security-repetitions", type=int, default=3, help="Repetitions for the security experiment suite.")
    parser.add_argument("--output-json", required=True, help="Path to output metrics JSON.")
    parser.add_argument("--output-markdown", required=True, help="Path to output metrics markdown.")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    metrics = collect_resume_metrics(
        args.benchmark_artifact,
        args.runs_root,
        provider_experiments=args.provider_experiments,
        memory_repetitions=args.memory_repetitions,
        large_memory_repetitions=args.large_memory_repetitions,
        context_repetitions=args.context_repetitions,
        security_repetitions=args.security_repetitions,
        experiment_mode=args.experiment_mode,
        real_provider=args.real_provider,
    )

    output_json = Path(args.output_json)
    output_md = Path(args.output_markdown)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)

    output_json.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_md.write_text(render_resume_metrics_markdown(metrics) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
