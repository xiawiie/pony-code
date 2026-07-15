#!/usr/bin/env python3
"""Aggregate a controller-anchored Docker Sandbox D7 evidence matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

from pico import sandbox_release_authority as release_authority

try:
    from scripts import docker_sandbox_release as release
except ModuleNotFoundError:
    import docker_sandbox_release as release


class AggregateError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_json(path, error_code):
    path = Path(path)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > release.MAX_ARTIFACT_BYTES
        ):
            raise AggregateError(error_code)
        remaining = before.st_size
        chunks = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise AggregateError(error_code)
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AggregateError(error_code)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        current = path.lstat()
        if _identity(before) != _identity(after) or _identity(after) != _identity(current):
            raise AggregateError(error_code)
        return release._decode_json(raw), "sha256:" + hashlib.sha256(raw).hexdigest()
    except AggregateError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AggregateError(error_code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _expected(path, anchored_digest):
    try:
        envelope, _raw_digest = _read_json(path, "expected_manifest_invalid")
        value = release_authority.verify_signed_envelope(
            envelope,
            purpose=release_authority.EXPECTED_MANIFEST_PURPOSE,
        )
        release.validate_expected_manifest(value)
        digest = release.expected_manifest_digest(value)
    except AggregateError:
        raise
    except (ValueError, release_authority.ReleaseAuthorityError) as exc:
        raise AggregateError("expected_manifest_invalid") from exc
    if anchored_digest != digest:
        raise AggregateError("expected_manifest_digest_mismatch")
    return value, digest


def _candidate_smoke_expected(path, anchored_digest):
    try:
        envelope, _raw_digest = _read_json(path, "expected_manifest_invalid")
        value = release_authority.verify_signed_envelope(
            envelope,
            purpose=release_authority.CANDIDATE_SMOKE_EXPECTED_PURPOSE,
        )
        release.validate_candidate_smoke_expected_manifest(value)
        digest = release.candidate_smoke_expected_digest(value)
    except AggregateError:
        raise
    except (ValueError, release_authority.ReleaseAuthorityError) as exc:
        raise AggregateError("expected_manifest_invalid") from exc
    if anchored_digest != digest:
        raise AggregateError("expected_manifest_digest_mismatch")
    return value, digest


def _artifact(path):
    value, raw_digest = _read_json(path, "release_artifact_invalid")
    try:
        release.validate_artifact(
            value,
            require_pass=True,
            require_release_binding=True,
        )
    except ValueError as exc:
        raise AggregateError("release_artifact_invalid") from exc
    return value, raw_digest


def _candidate_smoke_artifact(path):
    value, raw_digest = _read_json(path, "release_artifact_invalid")
    try:
        release.validate_candidate_smoke_artifact(value, require_pass=True)
    except ValueError as exc:
        raise AggregateError("release_artifact_invalid") from exc
    return value, raw_digest


def _matches_expected(value, expected, job, expected_digest):
    binding = value["release_binding"]
    return (
        binding["expected_manifest_digest"] == expected_digest
        and binding["release_nonce"] == expected["release_nonce"]
        and binding["job_id"] == job["job_id"]
        and binding["commit"] == expected["commit"]
        and binding["sdist_sha256"] == expected["sdist_sha256"]
        and binding["run_kind"] == job["run_kind"]
        and binding["run_index"] == job["run_index"]
    )


def _artifact_mapping(assignments):
    artifacts = {}
    for job_id, path in assignments:
        if not job_id or not path:
            raise AggregateError("release_artifact_mapping_invalid")
        if job_id in artifacts:
            raise AggregateError("release_job_duplicate")
        artifacts[job_id] = path
    return artifacts


def _require_expected_artifacts(artifacts, jobs):
    if not isinstance(artifacts, dict) or any(
        not isinstance(job_id, str)
        or not job_id
        or not isinstance(path, (str, os.PathLike))
        or not os.fspath(path)
        for job_id, path in artifacts.items()
    ):
        raise AggregateError("release_artifact_mapping_invalid")
    expected_ids = {job["job_id"] for job in jobs}
    actual_ids = set(artifacts)
    if actual_ids - expected_ids:
        raise AggregateError("release_job_unexpected")
    if expected_ids - actual_ids:
        raise AggregateError("release_matrix_incomplete")


def aggregate(expected_path, anchored_digest, artifacts):
    expected, expected_digest = _expected(expected_path, anchored_digest)
    _require_expected_artifacts(artifacts, expected["jobs"])
    jobs = {job["job_id"]: job for job in expected["jobs"]}
    rows = {}
    installed_tree_digest = None
    for assigned_job_id, path in artifacts.items():
        value, artifact_digest = _artifact(path)
        binding = value["release_binding"]
        job = jobs[assigned_job_id]
        if binding["job_id"] != assigned_job_id:
            raise AggregateError("release_binding_mismatch")
        if not _matches_expected(value, expected, job, expected_digest):
            raise AggregateError("release_binding_mismatch")
        if (
            value["distribution_sha256"] != expected["distribution_sha256"]
            or value["image_set_digest"] != expected["image_set_digest"]
            or value["policy_digest"] != expected["policy_digest"]
            or value["corpus_digest"] != expected["corpus_digest"]
        ):
            raise AggregateError("release_identity_mismatch")
        expected_image = next(
            item
            for item in expected["images"]
            if item["architecture"] == job["architecture"]
        )
        if value["image_digest"] != expected_image["image_digest"]:
            raise AggregateError("release_image_identity_mismatch")
        if (
            value["platform"] != job["platform"]
            or value["architecture"] != job["architecture"]
            or value["engine_profile"] != job["engine_profile"]
        ):
            raise AggregateError("release_job_identity_mismatch")
        if (
            value["prepare_network_performed"] is not (job["run_kind"] == "clean")
            or value["runtime_network_performed"] is not False
            or value["state_mutation_performed"] is not True
            or value["container_calls"] <= 0
            or value["target_started_count"] <= 0
            or value["target_started_count"] > value["container_calls"]
            or value["host_fallback_count"] != 0
            or value["residue_count"] != 0
        ):
            raise AggregateError("release_evidence_incomplete")
        try:
            gate_results = release._validate_case_evidence(
                value["case_evidence"],
                value["release_binding"],
                artifact_status=value["status"],
            )
        except ValueError as exc:
            raise AggregateError("release_evidence_incomplete") from exc
        if not gate_results or not all(gate_results.values()):
            raise AggregateError("release_evidence_incomplete")
        if installed_tree_digest is None:
            installed_tree_digest = value["installed_tree_digest"]
        elif value["installed_tree_digest"] != installed_tree_digest:
            raise AggregateError("installed_tree_identity_mismatch")
        rows[assigned_job_id] = {
            **job,
            "artifact_sha256": artifact_digest,
            "container_calls": value["container_calls"],
            "target_started_count": value["target_started_count"],
            "prepare_network_performed": value["prepare_network_performed"],
        }
    return {
        "record_type": "docker_sandbox_release_aggregate",
        "format_version": 1,
        "status": "passed",
        "reason_code": "complete_expected_matrix",
        "release_nonce": expected["release_nonce"],
        "commit": expected["commit"],
        "expected_manifest_digest": expected_digest,
        "distribution_sha256": expected["distribution_sha256"],
        "sdist_sha256": expected["sdist_sha256"],
        "installed_tree_digest": installed_tree_digest,
        "image_set_digest": expected["image_set_digest"],
        "images": expected["images"],
        "policy_digest": expected["policy_digest"],
        "corpus_digest": expected["corpus_digest"],
        "mandatory_artifact_count": len(rows),
        "host_fallback_count": 0,
        "residue_count": 0,
        "product_enablement": False,
        "jobs": [rows[job["job_id"]] for job in expected["jobs"]],
    }


def _candidate_inputs(candidate_path, production_aggregate_path, expected):
    try:
        candidate, _raw_digest = _read_json(
            candidate_path,
            "candidate_attestation_invalid",
        )
        candidate_payload = release_authority.verify_signed_envelope(
            candidate,
            purpose=release_authority.CANDIDATE_ATTESTATION_PURPOSE,
        )
        release_authority.validate_candidate_attestation_payload(candidate_payload)
        candidate_digest = release_authority.attestation_digest(candidate)
        production, _raw_digest = _read_json(
            production_aggregate_path,
            "production_aggregate_invalid",
        )
        production_digest = release_authority.canonical_digest(production)
    except AggregateError:
        raise
    except (ValueError, release_authority.ReleaseAuthorityError) as exc:
        raise AggregateError("candidate_attestation_invalid") from exc
    production_fields = {
        "record_type",
        "format_version",
        "status",
        "reason_code",
        "release_nonce",
        "commit",
        "expected_manifest_digest",
        "distribution_sha256",
        "sdist_sha256",
        "installed_tree_digest",
        "image_set_digest",
        "images",
        "policy_digest",
        "corpus_digest",
        "mandatory_artifact_count",
        "host_fallback_count",
        "residue_count",
        "product_enablement",
        "jobs",
    }
    if (
        not isinstance(production, dict)
        or set(production) != production_fields
        or production["record_type"] != "docker_sandbox_release_aggregate"
        or production["format_version"] != 1
        or production["status"] != "passed"
        or production["reason_code"] != "complete_expected_matrix"
        or production["mandatory_artifact_count"] != 92
        or production["host_fallback_count"] != 0
        or production["residue_count"] != 0
        or production["product_enablement"] is not False
        or not isinstance(production["jobs"], list)
        or len(production["jobs"]) != 92
        or candidate_digest != expected["candidate_attestation_digest"]
        or production_digest != expected["production_aggregate_digest"]
        or candidate_payload["production_aggregate_digest"] != production_digest
        or candidate_payload["candidate_nonce"] != expected["candidate_nonce"]
        or candidate_payload["release_nonce"] != expected["release_nonce"]
        or candidate_payload["commit"] != expected["commit"]
        or candidate_payload["expected_manifest_digest"]
        != production["expected_manifest_digest"]
        or production["release_nonce"] != expected["release_nonce"]
        or production["commit"] != expected["commit"]
        or any(
            candidate_payload[name] != expected[name]
            or production[name] != expected[name]
            for name in (
                "distribution_sha256",
                "sdist_sha256",
                "image_set_digest",
                "policy_digest",
                "corpus_digest",
            )
        )
        or production["images"] != expected["images"]
        or candidate_payload["installed_tree_digest"]
        != production["installed_tree_digest"]
    ):
        raise AggregateError("candidate_release_chain_mismatch")
    return candidate_payload, candidate_digest, production, production_digest


def aggregate_candidate_smoke(
    expected_path,
    anchored_digest,
    artifacts,
    *,
    candidate_attestation_path,
    production_aggregate_path,
):
    expected, expected_digest = _candidate_smoke_expected(
        expected_path,
        anchored_digest,
    )
    candidate, candidate_digest, production, production_digest = _candidate_inputs(
        candidate_attestation_path,
        production_aggregate_path,
        expected,
    )
    _require_expected_artifacts(artifacts, expected["jobs"])
    jobs = {job["job_id"]: job for job in expected["jobs"]}
    rows = {}
    for assigned_job_id, path in artifacts.items():
        value, artifact_digest = _candidate_smoke_artifact(path)
        binding = value["release_binding"]
        job = jobs[assigned_job_id]
        expected_image = next(
            item
            for item in expected["images"]
            if item["architecture"] == job["architecture"]
        )
        if (
            binding["job_id"] != assigned_job_id
            or binding != release.candidate_smoke_binding(expected, assigned_job_id)
            or value["platform"] != job["platform"]
            or value["architecture"] != job["architecture"]
            or value["engine_profile"] != job["engine_profile"]
            or value["distribution_sha256"] != expected["distribution_sha256"]
            or value["installed_tree_digest"]
            != candidate["installed_tree_digest"]
            or value["image_set_digest"] != expected["image_set_digest"]
            or value["image_digest"] != expected_image["image_digest"]
            or value["policy_digest"] != expected["policy_digest"]
            or value["corpus_digest"] != expected["corpus_digest"]
            or value["production_aggregate_digest"] != production_digest
            or value["candidate_attestation_digest"] != candidate_digest
        ):
            raise AggregateError("release_binding_mismatch")
        rows[assigned_job_id] = {
            **job,
            "artifact_sha256": artifact_digest,
            "image_digest": value["image_digest"],
        }
    return {
        "record_type": "docker_sandbox_candidate_smoke_aggregate",
        "format_version": 1,
        "status": "passed",
        "reason_code": "complete_candidate_smoke_matrix",
        "release_nonce": expected["release_nonce"],
        "candidate_nonce": expected["candidate_nonce"],
        "commit": expected["commit"],
        "expected_manifest_digest": expected_digest,
        "distribution_sha256": expected["distribution_sha256"],
        "sdist_sha256": expected["sdist_sha256"],
        "installed_tree_digest": production["installed_tree_digest"],
        "image_set_digest": expected["image_set_digest"],
        "images": expected["images"],
        "policy_digest": expected["policy_digest"],
        "corpus_digest": expected["corpus_digest"],
        "production_aggregate_digest": production_digest,
        "candidate_attestation_digest": candidate_digest,
        "mandatory_artifact_count": len(rows),
        "host_fallback_count": 0,
        "residue_count": 0,
        "product_enablement": False,
        "jobs": [rows[job["job_id"]] for job in expected["jobs"]],
    }


def _failed(reason_code):
    return {
        "record_type": "docker_sandbox_release_aggregate",
        "format_version": 1,
        "status": "failed",
        "reason_code": reason_code,
        "product_enablement": False,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected")
    parser.add_argument("--expected-digest")
    parser.add_argument(
        "--matrix",
        choices=("production", "candidate-smoke"),
        default="production",
    )
    parser.add_argument("--candidate-attestation")
    parser.add_argument("--production-aggregate")
    parser.add_argument(
        "--artifact",
        action="append",
        nargs=2,
        default=[],
        metavar=("JOB_ID", "PATH"),
        dest="artifacts",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.expected or not args.expected_digest:
        result = _failed("release_authority_unconfigured")
        print(json.dumps(result, sort_keys=True))
        return 3
    try:
        artifacts = _artifact_mapping(args.artifacts)
        if args.matrix == "candidate-smoke":
            if not args.candidate_attestation or not args.production_aggregate:
                raise AggregateError("release_authority_unconfigured")
            result = aggregate_candidate_smoke(
                args.expected,
                args.expected_digest,
                artifacts,
                candidate_attestation_path=args.candidate_attestation,
                production_aggregate_path=args.production_aggregate,
            )
        else:
            result = aggregate(args.expected, args.expected_digest, artifacts)
    except AggregateError as exc:
        result = _failed(exc.code)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
