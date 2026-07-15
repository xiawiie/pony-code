#!/usr/bin/env python3
"""Aggregate the four mandatory Sandbox F0 platform artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat

try:
    from scripts.srt_feasibility import MANDATORY_CHECK_IDS
except ModuleNotFoundError:
    from srt_feasibility import MANDATORY_CHECK_IDS


MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
EXPECTED_PLATFORMS = {
    ("darwin", "arm64"),
    ("darwin", "x64"),
    ("linux", "arm64"),
    ("linux", "x64"),
}
_HEX_40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class AggregateError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _object_from_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise AggregateError("artifact_invalid")
        value[key] = item
    return value


def _read_artifact(path):
    path = Path(path)
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > MAX_ARTIFACT_BYTES
        ):
            raise AggregateError("artifact_invalid")
        raw = path.read_bytes()
        report = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_from_pairs)
    except AggregateError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AggregateError("artifact_invalid") from exc
    return report, hashlib.sha256(raw).hexdigest()


def _validate_report(report):
    expected_fields = {
        "record_type", "format_version", "platform", "architecture", "mode",
        "status", "reason_code", "candidate", "harness", "versions", "checks",
        "mandatory_passed", "mandatory_failed", "host_fallback_count", "runs",
        "passed_runs", "failed_runs",
    }
    if not isinstance(report, dict) or set(report) != expected_fields:
        raise AggregateError("artifact_schema_invalid")
    if (
        report["record_type"] != "srt_feasibility"
        or type(report["format_version"]) is not int
        or report["format_version"] != 1
        or type(report["platform"]) is not str
        or type(report["architecture"]) is not str
        or type(report["mode"]) is not str
        or type(report["status"]) is not str
        or type(report["reason_code"]) is not str
    ):
        raise AggregateError("artifact_schema_invalid")
    platform_id = (report["platform"], report["architecture"])
    if (
        platform_id not in EXPECTED_PLATFORMS
        or report["mode"] != "real"
        or report["status"] != "passed"
        or report["reason_code"] != "mandatory_checks_passed"
    ):
        raise AggregateError("artifact_not_passed")
    candidate = report["candidate"]
    if (
        not isinstance(candidate, dict)
        or set(candidate)
        != {"node_version", "srt_package", "srt_version", "srt_integrity"}
        or any(type(value) is not str or not value for value in candidate.values())
        or candidate["srt_package"] != "@anthropic-ai/sandbox-runtime"
        or not candidate["srt_integrity"].startswith("sha512-")
    ):
        raise AggregateError("candidate_identity_invalid")
    harness = report["harness"]
    if (
        not isinstance(harness, dict)
        or set(harness) != {"commit", "digest", "dirty"}
        or type(harness["commit"]) is not str
        or _HEX_40_RE.fullmatch(harness["commit"]) is None
        or type(harness["digest"]) is not str
        or _SHA256_RE.fullmatch(harness["digest"]) is None
        or harness["dirty"] is not False
    ):
        raise AggregateError("harness_identity_invalid")
    versions = report["versions"]
    if (
        not isinstance(versions, dict)
        or set(versions)
        != {"node_candidate", "srt_candidate", "node_actual", "srt_actual"}
        or versions["node_candidate"] != candidate["node_version"]
        or versions["node_actual"] != candidate["node_version"]
        or versions["srt_candidate"] != candidate["srt_version"]
        or versions["srt_actual"] != candidate["srt_version"]
    ):
        raise AggregateError("candidate_version_mismatch")
    checks = report["checks"]
    if (
        not isinstance(checks, list)
        or tuple(
            item.get("check_id") for item in checks if isinstance(item, dict)
        )
        != MANDATORY_CHECK_IDS
        or any(
            set(item) != {"check_id", "mandatory", "status", "reason_code"}
            or item["mandatory"] is not True
            or item["status"] != "pass"
            or type(item["reason_code"]) is not str
            or not item["reason_code"]
            for item in checks
        )
    ):
        raise AggregateError("mandatory_checks_incomplete")
    if (
        type(report["mandatory_passed"]) is not int
        or report["mandatory_passed"] != len(MANDATORY_CHECK_IDS)
        or type(report["mandatory_failed"]) is not int
        or report["mandatory_failed"] != 0
        or type(report["host_fallback_count"]) is not int
        or report["host_fallback_count"] != 0
        or type(report["runs"]) is not int
        or report["runs"] < 1
        or type(report["passed_runs"]) is not int
        or report["passed_runs"] != report["runs"]
        or type(report["failed_runs"]) is not int
        or report["failed_runs"] != 0
    ):
        raise AggregateError("mandatory_evidence_incomplete")
    return platform_id, candidate, harness


def aggregate(artifacts):
    if len(artifacts) != len(EXPECTED_PLATFORMS):
        raise AggregateError("artifact_set_incomplete")
    rows = []
    seen = set()
    candidate = None
    harness = None
    for artifact in artifacts:
        report, digest = _read_artifact(artifact)
        platform_id, report_candidate, report_harness = _validate_report(report)
        if platform_id in seen:
            raise AggregateError("artifact_set_incomplete")
        if candidate is not None and report_candidate != candidate:
            raise AggregateError("candidate_identity_mismatch")
        if harness is not None and report_harness != harness:
            raise AggregateError("harness_identity_mismatch")
        seen.add(platform_id)
        candidate = report_candidate
        harness = report_harness
        rows.append({
            "platform": platform_id[0],
            "architecture": platform_id[1],
            "artifact_sha256": "sha256:" + digest,
            "runs": report["runs"],
        })
    if seen != EXPECTED_PLATFORMS:
        raise AggregateError("artifact_set_incomplete")
    rows.sort(key=lambda item: (item["platform"], item["architecture"]))
    return {
        "record_type": "srt_feasibility_aggregate",
        "format_version": 1,
        "status": "passed",
        "reason_code": "feasibility_approved",
        "feasibility_approval": True,
        "candidate": candidate,
        "harness": harness,
        "platforms": rows,
        "mandatory_check_ids": list(MANDATORY_CHECK_IDS),
    }


def _failed_report(reason_code):
    return {
        "record_type": "srt_feasibility_aggregate",
        "format_version": 1,
        "status": "failed",
        "reason_code": reason_code,
        "feasibility_approval": False,
        "candidate": {},
        "harness": {},
        "platforms": [],
        "mandatory_check_ids": list(MANDATORY_CHECK_IDS),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="*")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    args = parser.parse_args(argv)
    try:
        report = aggregate(args.artifacts)
    except AggregateError as exc:
        report = _failed_report(exc.code)
    if args.format == "json":
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    else:
        print(
            f"SRT feasibility aggregate: {report['status']}\n"
            f"reason: {report['reason_code']}\n",
            end="",
        )
    return 0 if report["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
