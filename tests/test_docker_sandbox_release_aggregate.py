import json
from types import SimpleNamespace

import pytest

from scripts import aggregate_docker_sandbox_release as aggregate
from scripts import docker_sandbox_release as release
from tests.release_authority_fixture import (
    candidate_smoke_expected_manifest,
    configure_test_authority,
    expected_manifest,
    runtime_case_rows,
    signed_candidate_smoke_expected_envelope,
    signed_chain_candidate_envelope,
    signed_expected_envelope,
)


def _sha(value):
    return "sha256:" + value * 64


def _expected_manifest():
    return expected_manifest()


@pytest.fixture(autouse=True)
def _trusted_test_release_key(monkeypatch):
    configure_test_authority(monkeypatch)


def _passed_artifact(expected, job):
    expected_image = next(
        item for item in expected["images"] if item["architecture"] == job["architecture"]
    )
    artifact = release._base_artifact(
        expected["distribution_sha256"],
        _sha("5"),
        SimpleNamespace(
            image_set_digest=expected["image_set_digest"],
            reference=expected_image["image_digest"],
            policy_digest=expected["policy_digest"],
        ),
    )
    artifact.update(
        {
            "status": "passed",
            "reason_code": "mandatory_checks_passed",
            "platform": job["platform"],
            "architecture": job["architecture"],
            "engine_profile": job["engine_profile"],
            "mandatory_passed": len(release.MANDATORY_CHECK_IDS),
            "mandatory_failed": 0,
            "container_calls": 13,
            "target_started_count": 12,
            "prepare_network_performed": job["run_kind"] == "clean",
            "state_mutation_performed": True,
            "release_binding": release.release_binding(expected, job["job_id"]),
        }
    )
    for check in artifact["checks"]:
        check.update(status="pass", reason_code="verified")
    release._set_case_evidence(
        artifact,
        "complete",
        "verified",
        runtime_case_rows(),
    )
    return artifact


def _write_matrix(tmp_path):
    expected = _expected_manifest()
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(
        json.dumps(signed_expected_envelope(), sort_keys=True),
        encoding="utf-8",
    )
    artifacts = {}
    for job in expected["jobs"]:
        path = tmp_path / f"{job['job_id']}.json"
        path.write_text(
            json.dumps(_passed_artifact(expected, job), sort_keys=True),
            encoding="utf-8",
        )
        artifacts[job["job_id"]] = path
    return expected, expected_path, artifacts


def test_expected_release_matrix_is_fixed_and_exact():
    jobs = release.expected_release_jobs()

    assert len(jobs) == 4 * (3 + 20)
    assert len({job["job_id"] for job in jobs}) == len(jobs)
    assert {job["run_kind"] for job in jobs} == {"clean", "soak"}
    assert release.validate_expected_manifest(_expected_manifest())["jobs"] == list(jobs)

    poisoned = _expected_manifest()
    poisoned["unexpected"] = True
    with pytest.raises(ValueError, match="expected release manifest"):
        release.validate_expected_manifest(poisoned)


def test_release_binding_is_required_for_d7_but_not_local_d6_artifacts():
    expected = _expected_manifest()
    job = expected["jobs"][0]
    artifact = _passed_artifact(expected, job)

    assert release.validate_artifact(
        artifact,
        require_pass=True,
        require_release_binding=True,
    )["release_binding"]["job_id"] == job["job_id"]

    artifact["release_binding"] = release.unbound_release_binding()
    release._set_case_evidence(
        artifact,
        "complete",
        "verified",
        artifact["case_evidence"]["cases"],
    )
    release.validate_artifact(artifact, require_pass=True)
    with pytest.raises(ValueError, match="release binding"):
        release.validate_artifact(artifact, require_release_binding=True)


def test_installed_worker_resolves_release_binding_from_anchored_inputs(tmp_path):
    expected = _expected_manifest()
    sdist = tmp_path / "pico.tar.gz"
    sdist.write_bytes(b"sdist")
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(json.dumps(signed_expected_envelope()), encoding="utf-8")
    job = expected["jobs"][0]
    args = SimpleNamespace(
        release_expected=str(expected_path),
        expected_digest=release.expected_manifest_digest(expected),
        release_job_id=job["job_id"],
        sdist=str(sdist),
    )
    image = SimpleNamespace(
        image_set_digest=expected["image_set_digest"],
        platform=expected["images"][0]["platform"],
        architecture=expected["images"][0]["architecture"],
        reference=expected["images"][0]["image_digest"],
        image_id=expected["images"][0]["image_id"],
        registry_reference=expected["images"][0]["registry_reference"],
        policy_digest=expected["policy_digest"],
        corpus_digest=expected["corpus_digest"],
    )

    binding, resolved_job = release._resolve_release_input(
        args,
        expected["distribution_sha256"],
        image,
        platform_identity=("darwin", "arm64"),
    )

    assert binding == release.release_binding(expected, job["job_id"])
    assert resolved_job == job

    args.expected_digest = _sha("0")
    with pytest.raises(ValueError, match="release input mismatch"):
        release._resolve_release_input(
            args,
            expected["distribution_sha256"],
            image,
            platform_identity=("darwin", "arm64"),
        )


def test_installed_worker_rejects_partial_or_wrong_platform_release_input(tmp_path):
    empty = SimpleNamespace(
        release_expected=None,
        expected_digest=None,
        release_job_id=None,
        sdist=None,
    )
    image = SimpleNamespace(
        image_set_digest=_sha("f"),
        platform="linux/arm64",
        architecture="arm64",
        reference=_sha("3"),
        image_id=_sha("c"),
        registry_reference="registry.example/pico@" + _sha("3"),
        policy_digest=_sha("4"),
        corpus_digest=release.CORPUS_DIGEST,
    )
    assert release._resolve_release_input(empty, _sha("1"), image) == (
        release.unbound_release_binding(),
        None,
    )

    empty.release_job_id = "d7-darwin-arm64-clean-01"
    with pytest.raises(ValueError, match="release input mismatch"):
        release._resolve_release_input(empty, _sha("1"), image)

    expected, expected_path, _artifacts = _write_matrix(tmp_path)
    sdist = tmp_path / "pico.tar.gz"
    sdist.write_bytes(b"sdist")
    args = SimpleNamespace(
        release_expected=str(expected_path),
        expected_digest=release.expected_manifest_digest(expected),
        release_job_id=expected["jobs"][0]["job_id"],
        sdist=str(sdist),
    )
    assert release._resolve_release_input(
        args,
        expected["distribution_sha256"],
        image,
        platform_identity=("darwin", "arm64"),
    )[1] == expected["jobs"][0]
    with pytest.raises(ValueError, match="release input mismatch"):
        release._resolve_release_input(
            args,
            expected["distribution_sha256"],
            image,
            platform_identity=("linux", "arm64"),
        )


def test_d7_aggregate_requires_the_complete_controller_expected_matrix(tmp_path):
    expected, expected_path, artifacts = _write_matrix(tmp_path)
    expected_digest = release.expected_manifest_digest(expected)

    result = aggregate.aggregate(expected_path, expected_digest, artifacts)

    assert result["status"] == "passed"
    assert result["reason_code"] == "complete_expected_matrix"
    assert result["expected_manifest_digest"] == expected_digest
    assert result["mandatory_artifact_count"] == 92
    assert result["installed_tree_digest"] == _sha("5")
    assert result["image_set_digest"] == expected["image_set_digest"]
    assert result["images"] == expected["images"]
    assert result["host_fallback_count"] == 0
    assert result["residue_count"] == 0
    assert result["product_enablement"] is False
    assert [row["job_id"] for row in result["jobs"]] == [
        job["job_id"] for job in expected["jobs"]
    ]


def test_d7_aggregate_cli_uses_controller_owned_artifact_slots(tmp_path, capsys):
    expected, expected_path, artifacts = _write_matrix(tmp_path)
    argv = [
        "--expected",
        str(expected_path),
        "--expected-digest",
        release.expected_manifest_digest(expected),
    ]
    for job_id, path in artifacts.items():
        argv.extend(("--artifact", job_id, str(path)))

    assert aggregate.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    (
        (
            lambda value, _expected: value["release_binding"].__setitem__(
                "release_nonce", "c" * 64
            ),
            "release_binding_mismatch",
        ),
        (
            lambda value, _expected: value.__setitem__(
                "distribution_sha256", _sha("9")
            ),
            "release_identity_mismatch",
        ),
        (
            lambda value, expected: value.__setitem__(
                "image_digest", expected["images"][0]["image_digest"]
            ),
            "release_image_identity_mismatch",
        ),
        (
            lambda value, expected: value.__setitem__(
                "release_binding",
                release.release_binding(expected, expected["jobs"][0]["job_id"]),
            ),
            "release_binding_mismatch",
        ),
    ),
)
def test_d7_aggregate_rejects_mixed_replayed_or_duplicate_artifacts(
    tmp_path,
    mutation,
    reason_code,
):
    expected, expected_path, artifacts = _write_matrix(tmp_path)
    path = artifacts[expected["jobs"][-1]["job_id"]]
    value = json.loads(path.read_text(encoding="utf-8"))
    mutation(value, expected)
    release._set_case_evidence(
        value,
        value["case_evidence"]["execution_status"],
        value["case_evidence"]["reason_code"],
        value["case_evidence"]["cases"],
    )
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    with pytest.raises(aggregate.AggregateError) as caught:
        aggregate.aggregate(
            expected_path,
            release.expected_manifest_digest(expected),
            artifacts,
        )

    assert caught.value.code == reason_code


def test_d7_aggregate_rejects_wrong_anchor_missing_job_and_symlink(tmp_path):
    expected, expected_path, artifacts = _write_matrix(tmp_path)
    digest = release.expected_manifest_digest(expected)

    with pytest.raises(aggregate.AggregateError) as caught:
        aggregate.aggregate(expected_path, _sha("0"), artifacts)
    assert caught.value.code == "expected_manifest_digest_mismatch"

    with pytest.raises(aggregate.AggregateError) as caught:
        aggregate.aggregate(expected_path, digest, dict(list(artifacts.items())[:-1]))
    assert caught.value.code == "release_matrix_incomplete"

    linked = tmp_path / "linked.json"
    first_job_id = expected["jobs"][0]["job_id"]
    linked.symlink_to(artifacts[first_job_id])
    poisoned = dict(artifacts)
    poisoned[first_job_id] = linked
    with pytest.raises(aggregate.AggregateError) as caught:
        aggregate.aggregate(expected_path, digest, poisoned)
    assert caught.value.code == "release_artifact_invalid"


def test_d7_aggregate_cli_fails_closed_without_release_authority(capsys):
    assert aggregate.main([]) == 3

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "format_version": 1,
        "product_enablement": False,
        "reason_code": "release_authority_unconfigured",
        "record_type": "docker_sandbox_release_aggregate",
        "status": "failed",
    }


def _write_candidate_smoke_chain(tmp_path):
    expected, expected_path, production_artifacts = _write_matrix(tmp_path)
    production = aggregate.aggregate(
        expected_path,
        release.expected_manifest_digest(expected),
        production_artifacts,
    )
    production_path = tmp_path / "production.json"
    production_path.write_text(json.dumps(production, sort_keys=True), encoding="utf-8")
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(
        json.dumps(signed_chain_candidate_envelope(), sort_keys=True),
        encoding="utf-8",
    )
    smoke_expected = candidate_smoke_expected_manifest()
    smoke_expected_path = tmp_path / "smoke-expected.json"
    smoke_expected_path.write_text(
        json.dumps(signed_candidate_smoke_expected_envelope(), sort_keys=True),
        encoding="utf-8",
    )
    artifacts = {}
    for job in smoke_expected["jobs"]:
        expected_image = next(
            item
            for item in smoke_expected["images"]
            if item["architecture"] == job["architecture"]
        )
        artifact = {
            "record_type": "docker_sandbox_candidate_public_smoke",
            "format_version": 1,
            "status": "passed",
            "reason_code": "public_cli_smoke_passed",
            "platform": job["platform"],
            "architecture": job["architecture"],
            "engine_profile": job["engine_profile"],
            "distribution_sha256": smoke_expected["distribution_sha256"],
            "installed_tree_digest": production["installed_tree_digest"],
            "image_set_digest": smoke_expected["image_set_digest"],
            "image_digest": expected_image["image_digest"],
            "policy_digest": smoke_expected["policy_digest"],
            "corpus_digest": smoke_expected["corpus_digest"],
            "production_aggregate_digest": smoke_expected[
                "production_aggregate_digest"
            ],
            "candidate_attestation_digest": smoke_expected[
                "candidate_attestation_digest"
            ],
            "public_cli_exit_code": 0,
            "session_state": "discarded",
            "source_unchanged": True,
            "product_cache_written": False,
            "host_fallback_count": 0,
            "residue_count": 0,
            "release_binding": release.candidate_smoke_binding(
                smoke_expected,
                job["job_id"],
            ),
            "product_enablement": False,
        }
        path = tmp_path / f"{job['job_id']}.json"
        path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
        artifacts[job["job_id"]] = path
    return (
        smoke_expected,
        smoke_expected_path,
        artifacts,
        candidate_path,
        production_path,
    )


def test_candidate_smoke_aggregate_requires_complete_signed_chain(tmp_path):
    (
        expected,
        expected_path,
        artifacts,
        candidate_path,
        production_path,
    ) = _write_candidate_smoke_chain(tmp_path)

    result = aggregate.aggregate_candidate_smoke(
        expected_path,
        release.candidate_smoke_expected_digest(expected),
        artifacts,
        candidate_attestation_path=candidate_path,
        production_aggregate_path=production_path,
    )

    assert result["status"] == "passed"
    assert result["reason_code"] == "complete_candidate_smoke_matrix"
    assert result["mandatory_artifact_count"] == 4
    assert result["product_enablement"] is False


def test_candidate_smoke_cli_uses_controller_owned_artifact_slots(tmp_path, capsys):
    (
        expected,
        expected_path,
        artifacts,
        candidate_path,
        production_path,
    ) = _write_candidate_smoke_chain(tmp_path)
    argv = [
        "--matrix",
        "candidate-smoke",
        "--expected",
        str(expected_path),
        "--expected-digest",
        release.candidate_smoke_expected_digest(expected),
        "--candidate-attestation",
        str(candidate_path),
        "--production-aggregate",
        str(production_path),
    ]
    for job_id, path in artifacts.items():
        argv.extend(("--artifact", job_id, str(path)))

    assert aggregate.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


def test_candidate_smoke_rejects_artifact_self_reported_for_another_slot(tmp_path):
    (
        expected,
        expected_path,
        artifacts,
        candidate_path,
        production_path,
    ) = _write_candidate_smoke_chain(tmp_path)
    assigned_job_id = expected["jobs"][-1]["job_id"]
    path = artifacts[assigned_job_id]
    value = json.loads(path.read_text(encoding="utf-8"))
    value["release_binding"] = release.candidate_smoke_binding(
        expected,
        expected["jobs"][0]["job_id"],
    )
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    with pytest.raises(aggregate.AggregateError) as caught:
        aggregate.aggregate_candidate_smoke(
            expected_path,
            release.candidate_smoke_expected_digest(expected),
            artifacts,
            candidate_attestation_path=candidate_path,
            production_aggregate_path=production_path,
        )

    assert caught.value.code == "release_binding_mismatch"


def test_candidate_smoke_worker_resolves_signed_inputs_before_public_cli(
    tmp_path,
    monkeypatch,
):
    (
        expected,
        expected_path,
        _artifacts,
        candidate_path,
        production_path,
    ) = _write_candidate_smoke_chain(tmp_path)
    candidate_path.chmod(0o600)
    sdist = tmp_path / "pico.tar.gz"
    sdist.write_bytes(b"sdist")
    monkeypatch.setattr(
        aggregate.release_authority,
        "installed_tree_digest",
        lambda _root, _version: _sha("5"),
    )
    monkeypatch.setattr(release, "_platform_identity", lambda: ("darwin", "arm64"))
    args = SimpleNamespace(
        candidate_smoke_expected=str(expected_path),
        candidate_smoke_expected_digest=release.candidate_smoke_expected_digest(
            expected
        ),
        candidate_smoke_job_id=expected["jobs"][0]["job_id"],
        candidate_attestation=str(candidate_path),
        production_aggregate=str(production_path),
        distribution_sha256=expected["distribution_sha256"],
        sdist=str(sdist),
    )
    image = SimpleNamespace(
        image_set_digest=expected["image_set_digest"],
        platform=expected["images"][0]["platform"],
        architecture=expected["images"][0]["architecture"],
        reference=expected["images"][0]["image_digest"],
        image_id=expected["images"][0]["image_id"],
        registry_reference=expected["images"][0]["registry_reference"],
        policy_digest=expected["policy_digest"],
        corpus_digest=expected["corpus_digest"],
    )

    resolved, job, candidate_digest = release._resolve_candidate_smoke_inputs(
        args,
        package_root=tmp_path,
        image=image,
    )

    assert resolved == expected
    assert job == expected["jobs"][0]
    assert candidate_digest == expected["candidate_attestation_digest"]


@pytest.mark.parametrize(
    ("target", "mutation", "code"),
    (
        (
            "artifact",
            lambda value: value.__setitem__("image_digest", _sha("3")),
            "release_binding_mismatch",
        ),
        (
            "candidate",
            lambda value: value["payload"].__setitem__("candidate_nonce", "d" * 64),
            "candidate_attestation_invalid",
        ),
        (
            "production",
            lambda value: value.__setitem__("residue_count", 1),
            "candidate_release_chain_mismatch",
        ),
    ),
)
def test_candidate_smoke_aggregate_rejects_mixed_chain(
    tmp_path,
    target,
    mutation,
    code,
):
    (
        expected,
        expected_path,
        artifacts,
        candidate_path,
        production_path,
    ) = _write_candidate_smoke_chain(tmp_path)
    path = {
        "artifact": artifacts[expected["jobs"][-1]["job_id"]],
        "candidate": candidate_path,
        "production": production_path,
    }[target]
    value = json.loads(path.read_text(encoding="utf-8"))
    mutation(value)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    with pytest.raises(aggregate.AggregateError) as caught:
        aggregate.aggregate_candidate_smoke(
            expected_path,
            release.candidate_smoke_expected_digest(expected),
            artifacts,
            candidate_attestation_path=candidate_path,
            production_aggregate_path=production_path,
        )

    assert caught.value.code == code
