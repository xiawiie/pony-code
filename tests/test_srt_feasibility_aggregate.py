import importlib.util
import json
from pathlib import Path

import pytest

from scripts.srt_feasibility import MANDATORY_CHECK_IDS


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "aggregate_srt_feasibility.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "aggregate_srt_feasibility",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(platform, architecture):
    candidate = {
        "node_version": "24.19.0",
        "srt_package": "@anthropic-ai/sandbox-runtime",
        "srt_version": "0.0.66",
        "srt_integrity": "sha512-candidate",
    }
    checks = [
        {
            "check_id": check_id,
            "mandatory": True,
            "status": "pass",
            "reason_code": "verified",
        }
        for check_id in MANDATORY_CHECK_IDS
    ]
    return {
        "record_type": "srt_feasibility",
        "format_version": 1,
        "platform": platform,
        "architecture": architecture,
        "mode": "real",
        "status": "passed",
        "reason_code": "mandatory_checks_passed",
        "candidate": candidate,
        "harness": {
            "commit": "a" * 40,
            "digest": "sha256:" + "b" * 64,
            "dirty": False,
        },
        "versions": {
            "node_candidate": candidate["node_version"],
            "srt_candidate": candidate["srt_version"],
            "node_actual": candidate["node_version"],
            "srt_actual": candidate["srt_version"],
        },
        "checks": checks,
        "mandatory_passed": len(checks),
        "mandatory_failed": 0,
        "host_fallback_count": 0,
        "runs": 3,
        "passed_runs": 3,
        "failed_runs": 0,
    }


def _artifacts(tmp_path):
    paths = []
    for platform, architecture in (
        ("darwin", "arm64"),
        ("darwin", "x64"),
        ("linux", "arm64"),
        ("linux", "x64"),
    ):
        path = tmp_path / f"{platform}-{architecture}.json"
        path.write_text(
            json.dumps(_report(platform, architecture)),
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def test_f0_aggregate_requires_four_matching_complete_platform_artifacts(tmp_path):
    module = _load_script()

    report = module.aggregate(_artifacts(tmp_path))

    assert report["status"] == "passed"
    assert report["reason_code"] == "feasibility_approved"
    assert report["feasibility_approval"] is True
    assert len(report["platforms"]) == 4
    assert report["mandatory_check_ids"] == list(MANDATORY_CHECK_IDS)
    assert all(
        item["artifact_sha256"].startswith("sha256:")
        for item in report["platforms"]
    )


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        (
            lambda report: report["candidate"].update(srt_version="0.0.67"),
            "candidate_version_mismatch",
        ),
        (
            lambda report: report["harness"].update(dirty=True),
            "harness_identity_invalid",
        ),
        (
            lambda report: report.update(host_fallback_count=1),
            "mandatory_evidence_incomplete",
        ),
        (
            lambda report: report["checks"][0].update(status="not_ready"),
            "mandatory_checks_incomplete",
        ),
    ],
)
def test_f0_aggregate_rejects_mixed_or_incomplete_evidence(
    tmp_path,
    mutation,
    reason_code,
):
    module = _load_script()
    paths = _artifacts(tmp_path)
    report = json.loads(paths[0].read_text(encoding="utf-8"))
    mutation(report)
    paths[0].write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(module.AggregateError) as caught:
        module.aggregate(paths)

    assert caught.value.code == reason_code


def test_f0_aggregate_rejects_candidate_or_harness_mixing(tmp_path):
    module = _load_script()
    paths = _artifacts(tmp_path)
    report = json.loads(paths[-1].read_text(encoding="utf-8"))
    report["candidate"]["srt_integrity"] = "sha512-other"
    paths[-1].write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(module.AggregateError) as caught:
        module.aggregate(paths)

    assert caught.value.code == "candidate_identity_mismatch"


def test_f0_aggregate_cli_fails_closed_without_exact_artifact_set(capsys):
    module = _load_script()

    assert module.main(["--format", "json"]) == 3

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["reason_code"] == "artifact_set_incomplete"
    assert report["feasibility_approval"] is False
