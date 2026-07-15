#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico.sandbox_linux import probe  # noqa: E402
from pico.sandbox_toolchain import SandboxToolchain  # noqa: E402


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Report Linux sandbox capabilities without changing the system.")
    parser.add_argument("--timeout", type=float, default=2.0, help="Per-probe timeout in seconds.")
    parser.add_argument("--format", choices=("text", "json"), default="json")
    return parser


def _managed_identity():
    try:
        root = Path.home().resolve(strict=True) / ".pico" / "toolchains" / "sandbox"
        toolchain = SandboxToolchain(root, create_root=False)
        if toolchain.status().get("status") == "ready":
            return toolchain.identity()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        pass
    return None


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than zero")
    capability_report = probe(
        timeout=args.timeout,
        sandbox_identity=_managed_identity(),
    )
    report = capability_report.to_dict()
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"Linux sandbox: {capability_report.status}\n"
            f"reason: {report.get('applicability_reason', 'unknown')}"
        )
    if capability_report.platform != "Linux":
        return 0
    return 0 if capability_report.status == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
