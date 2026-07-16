"""Non-production release key and signed expected manifest for tests."""

from datetime import datetime, timezone
from types import MappingProxyType

from pico import sandbox_release_authority as authority
from pico import docker_sandbox_network_control as network_control
from scripts import docker_sandbox_release as release


TEST_KEY_ID = "test-release-2026"
TEST_MODULUS = "rQNLE0HV9m4eks51n4xc7cO5lOq6In1lG99KJoxFswmWOTsda4EdGq0kEHfuxkp0ku-ZalxNyo1wctpUcb1zOnToCYZBxN1tbv1uZPi6IF0kocK2fNDxpqfL8qCTUBGflOvJx-VynDnYyUpwHPFTTBhg7aTHHTemykyTVB3RhQlAiaI5PyaQfOyJSTfpJhAyhSoWopWdUmEvjl-o84mB2Kj4MTQsqGkjJ2wr9hxARn3ktSVHbcgsexyP55uhTfv8k6yyWtfdUYnd-Jm9wXuVvKMEZrA7L6wpbkmlF_P_Xdjb0JWFXvXDVCd7hbx_RWnptimvDKo7RSii4bBdQp9wJJRQdLZHAB8gbLG2GgOUp_Lr09BdSQX0iZMt0mHdmqdA36pCnnj19s7yY_PClBj8wRukXRdbmN239EehrVEKW1LTKmSinlWhH8eiPhRyxA4jJ9poKRZ6kh-ZzyDnZ7vDCLW6GnmwOi-mNErTiQcaxwMGQTeY-bHiUdWGGiI4atfD"
TEST_EXPECTED_SIGNATURE = "oITRPJX4C9VQhgYrc8AsZMui2TNihU8VIKvh0zkekgPnJPKe-HGYlQ0Y444yOvci6GzjfFHLnIrASxmK8Zfx8iggN-iO0SvWEnzwHGMr79isP6UvsIs_TLBUSh5yHCwhwrsDPD3q02Zf_3jJXOQX9SVSrxqDJUwlm6CpyV-1owCkj9W9Yi4D4KT3xcuFDmUufjSnEfDHV6nhRoE85_lygpABdVf5m7bdJzubuRsDRlcC2w6w5cH_Unq3yaHi1a2bMI_EHnTx4PvPIBI0sK5dnNm9WF4_WnTto7-4wFdCzZcmpXtpVB3FAJC6w4VbsxOHWpdJFapFRIBUpKCdndfdvwTCVIALREt0ICfmdZoNOGxx6Qfeov_A3j69yMunD8SiTU39KUgIDWKwMrfmUOehAr2EsDMlSYlIBnQZelGpYGVdvyG0ViOxg9t6P99mYiSWQzqGI00CMa0MEJEQEJs-bZWUacv3SxOs9adXeHZFbdurkdgmUSPnAvzyDkHjlzPm"
TEST_CANDIDATE_SIGNATURE = "ADIdGlrG4JftMI0Jjznql9HLvnUwErwwPzu4bk-FVMXrHRos-CgH_TOOydd-iD1UvWrEandmdbdNyvRbNVne-wC_oiZRpybPEaasd1XnC1Vfaonv1s-ppefOEOI4cRBXODneX35isiDe-AUqXFWmroOxDDv5_CaSqTHbiU8zLpZF6fBO1GHmdTurMxOsmyxYAsUrWob3yUUsHTQ1e3X3giXaHznnD9n9Rx_mjPydxPA8qrcGfpylj_517RrL0J7w70mQTtTHmqfi7JJ-nXxzV6vhUCszNC6rHAkFwUFUS-3xX9vhqtXkvruixumc0tWxyqu3YfRLtALw9vaAe9AArX2Iowyn8VO-9Fc4M23rk53YRe9GvU8fkewcB86mfCAxLrnGXlEKSofwp4NOisyFz9VB01T3Q3lgvOBmqdr5Vo7n1gDj-5smWtUpMae2VPdcAYT4molaXyMyQWdCrbe9gDUecmB94k5tD3_Ck5D7Xo7wFL0FzXgOCyXV6ZT-Q9br"
TEST_PRODUCT_SIGNATURE = "XNBhSA22h0WX7nUxjeo-TpNCGmzDs17dDlkKg80Nb-157eXvqiqNnPrGzM26B1WfOD6lSXbNzyhYVh3IW3zLmUzeyAi99ukr0lSGqF2pbuX9sjUMre7VKagL-F0Iibd1J1rsL4WleCB8gBD4ZV9l1R_m2V7xRxkf62wRqiKxGzu6HCMp3kyUQl9vqURT-tTzi9_dXfDGKEkVyFL2IcBYjY7dUEs38z1GqqlfejJbHQ1zvqYC4-7H9xYPwJi6PnfqgsVGiEeaxwcQE4ld0-RVBZ7edFgNtqLmMmqbvfUHKmMUEIHQiIU8JwSmiGc-MwtTlLSwpdXgWinoqouvrcvHaNgpJtie912GPNKeILH8VWpyIXajImHiqyffAgUHAr9dgOEYxM82AINeNf8YkJWr212_XOCIxc8-WOe1mWGPWHAPIlBPqjokgS_7bSkEuuNt5i8tgncWUMnHluCXtY8Nfv-QLWG7ClfmRRN1UEV89GgM6q-ZyQZ-s1lOkIJDcFAa"
TEST_PRODUCT_SEQUENCE_2_SIGNATURE = "dz7j4DI9yVUtJwdzLK7ORhlLLTGX1WyHKoBC03HzdSx3oVrAnx0XXtIkKNspGDl4rFAupq87Ov_KJR26GkTGpxu8mEel6GGvneyGuaouxqm-4X6z6lJE-5J_BbIPP1JrOpx1qvkLxgqKbqjdYk-F-I0MWDAM896OwKuHwEq2h6wf7XCCjuc1EtNGsQ1S_g2fs17uWxLAkGXqfhESh6C2xQ-KFewBh3Mnk692Lxe2yt3o3OPz0CwT-C43PYXr97M2VRzASDl6ShQphaie4kD5-LhpL2rMzd1GCMsc0memMk4HdiMtmewDgCZt4xlwq3Exv2FAG4bbAYoM9pUprqdAQHZUrr_J9dP9kikK4eCpWE-n5PfPo7jDtdOLEU6ErNcJni12Ea9t9aZl5WJx2jVyFDuJ1_l9niZpr1sPmv3tS0tpuodTMlPGJv_-jd6Rz5b7pCfUXXVudIsDtRKpfEjg419YnbkfcvRnb9bnpMr3keZ7EN3T7BQQXGyhn_uj4qjb"
TEST_CHAIN_PRODUCTION_AGGREGATE_DIGEST = (
    "sha256:b71a5102cd56a2d6bf7d5c0e7aa5e6016a195760bc74552634d67380cab2ad5c"
)
TEST_CHAIN_CANDIDATE_DIGEST = (
    "sha256:2df9e59bafdf8a44744bd1fea95b7fbd0d1bf2e79274b8c3d5ae8521d05afaf5"
)
TEST_CHAIN_CANDIDATE_SIGNATURE = "kay3Iux4thGZNhNV71_iq-saKGX3Ygc4nTWedqB51Ge3HQQ6-84LzXaTj4WYq-GCNNDpW1BEs9wDIHbqsvwEhgKnxnhWJbpmWX6-KQhUYfa97zh3GjtNwxi9TTJ-WVPQLVC7TsEQENmUcTy_WbMWSZ1dA6PMQnLTpuyN_T_Rq-TNYLJcYIcr3_ZmQz-8Z59Kba38ZiobCI9SjkT_BaoSWlnAEFvaSqw3VzZjVKv4VrH7eFUzJzrFXNKCvkpdy9ASJ-BlTiijnO2_NSYL6YztlEwQQq-P99vRftOnHKZkOqOiSzvi9cGkmACfm1dQgnoizFDhxbvyEsvWas9qAQwAkEnYIijp_MzaKXz_aQ-rojO6W5uSt2vzThaFIwaNy1sHgOfRPwHJ-OG1R9_GawLs8qQM7SvIb2E09ZvPlObAXqTFui6gpk3PeZdw3mohdQg3qXxyBKICn7IKZGJ_Kcv4wSFsFIaADtZhlvTbTjtPPHpEMNGI1xLJ8HHdJvoVOVYh"
TEST_SMOKE_EXPECTED_SIGNATURE = "PIIZhU51cvzmERtIi4iHwp1wecD7PrPX9RQR--X_wGdA8KcZdXsKgyF-TvGUyPNhhI0ZLTHL6llg1ewtSp-_0CbZO6qKmwPlnbQkWT3yRARh9iCnExKxYwyeugqEnC6cbOb4ok24M25yLFPyVhWMI5z6CF4nHNlghC0p1Gz-BmiuKGE_7j5LyoUXVrHCnELcTbs8f1YsvsT1k-ley-puDnXW5_x5835PDr0xq2pav90mJ5TnBdJqZUZQ_AYSzSMWqxuawI1O1vTvmBOE3WPzAfJljFJfG8vvqC9l0SnH8obvdpjIwf1Q52KhcKbtLtgvt3c_xY2nXttiQIo_qKBcBaYAHxhtMvj7cxDp0WjIp6sEz8YKZtmCLYTFOdR-bZMjp7q28P8BHAD8PvsUYNtJqBF-shmYr8NbLzzBhH2mNTNcJyMlQSgnUjPJnf02fmDg-C60iLt5fteUWMpKfsY4Qd7Kc6E18_6lO0gbyaGmIrCJhA33oNJlTbyeBAYHfxtg"
TEST_KEYS = MappingProxyType(
    {
        TEST_KEY_ID: {
            "algorithm": authority.SIGNATURE_ALGORITHM,
            "modulus": TEST_MODULUS,
            "exponent": authority.RSA_PUBLIC_EXPONENT,
            "not_before": "2026-01-01T00:00:00Z",
            "not_after": "2030-01-01T00:00:00Z",
            "status": "active",
        }
    }
)
TEST_NOW = datetime(2026, 7, 14, 1, tzinfo=timezone.utc)
SDIST_DIGEST = "sha256:714772a9f82b2aeb4fa5f7092d00fe4ac4c9cdeb6800840b6ed39ea64c4d785a"


def runtime_case_rows():
    tool_facts = {
        "model_client": "FakeModelClient",
        "provider_transport_attempts": 0,
        "tool_sequence": ["read_file", "write_file", "run_shell", "read_file"],
        "tool_statuses": ["ok"] * 4,
        "tool_change_sequence": ["write_file", "run_shell"],
        "tool_change_statuses": ["finalized"] * 2,
        "initial_read_match": True,
        "builtin_write_a_match": True,
        "shell_observed_a": True,
        "final_read_b_match": True,
        "source_pre_apply_unchanged": True,
        "execution_plane": "sandbox",
        "sandbox_outcome": "completed",
        "exit_code": 0,
        "timed_out": False,
        "target_started": True,
        "runner_executed": True,
        "residue_detected": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "cleanup_status": "completed",
        "host_fallback_count": 0,
    }
    recovery_entries = [
        {
            "path": path,
            "decision": "restore",
            "reason": "hash_match",
            "change_kind": "created",
            "before_exists": False,
            "after_sha256": release._RUNTIME_CANDIDATE_HASHES[path],
            "snapshot_eligible": True,
            "source_tool": tool,
        }
        for path, tool in sorted(
            (
                (release._RUNTIME_CANDIDATE_A, "write_file"),
                (release._RUNTIME_CANDIDATE_B, "run_shell"),
            )
        )
    ]
    diff_entries = [
        {
            "path": path,
            "change_kind": "created",
            "classification": "candidate",
            "before_exists": False,
            "after_sha256": release._RUNTIME_CANDIDATE_HASHES[path],
            "size": len(content.encode("utf-8")),
            "blob_bound": True,
        }
        for path, content in sorted(
            (
                (release._RUNTIME_CANDIDATE_A, release._RUNTIME_CANDIDATE_A_CONTENT),
                (release._RUNTIME_CANDIDATE_B, release._RUNTIME_CANDIDATE_B_CONTENT),
            )
        )
    ]
    rows = [
        release._runtime_case_row("runtime.tool_roundtrip", tool_facts),
        release._runtime_case_row(
            "runtime.recovery_preview",
            {
                "checkpoint_type": "turn",
                "reference_graph_valid": True,
                "preview_status": "ready",
                "entries": recovery_entries,
            },
        ),
        release._runtime_case_row(
            "runtime.diff_apply_cleanup",
            {
                "diff_status": "diff_ready",
                "pre_apply_session_state": "pending_review",
                "source_pre_apply_unchanged": True,
                "entries": diff_entries,
                "apply_status": "apply_applied",
                "final_session_state": "applied",
                "cleanup_status": "complete",
                "lease_released": True,
                "execution_root_absent": True,
                "source_after": [
                    {
                        "path": entry["path"],
                        "sha256": entry["after_sha256"],
                        "size": entry["size"],
                    }
                    for entry in diff_entries
                ],
            },
        ),
    ]
    network_rows = release._network_case_rows(
        network_control._result(
            "sha256:" + "1" * 64,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            {name: True for name in network_control._FACT_FIELDS},
            network_control.NetworkControlCleanup("completed", True, 0, 0),
        ),
        {
            name: 0 if name.endswith("_remaining") else True
            for name in release._NETWORK_PROBE_FACT_FIELDS
        },
    )
    return sorted(
        [*release._passing_apply_case_rows(), *network_rows, *rows],
        key=lambda item: item["case_id"],
    )


def expected_manifest():
    return {
        "record_type": "docker_sandbox_release_expected",
        "format_version": 1,
        "release_nonce": "a" * 64,
        "commit": "b" * 40,
        "distribution_sha256": "sha256:" + "1" * 64,
        "sdist_sha256": SDIST_DIGEST,
        "image_set_digest": "sha256:" + "f" * 64,
        "images": [
            {
                "platform": "linux/arm64",
                "architecture": "arm64",
                "image_digest": "sha256:" + "3" * 64,
                "image_id": "sha256:" + "c" * 64,
                "registry_reference": "registry.example/pico@sha256:" + "3" * 64,
            },
            {
                "platform": "linux/amd64",
                "architecture": "amd64",
                "image_digest": "sha256:" + "d" * 64,
                "image_id": "sha256:" + "e" * 64,
                "registry_reference": "registry.example/pico@sha256:" + "d" * 64,
            },
        ],
        "policy_digest": "sha256:" + "4" * 64,
        "corpus_digest": release.CORPUS_DIGEST,
        "jobs": [dict(job) for job in release.expected_release_jobs()],
    }


def signed_expected_envelope():
    return {
        "record_type": "pico_signed_release_envelope",
        "format_version": 1,
        "purpose": authority.EXPECTED_MANIFEST_PURPOSE,
        "algorithm": authority.SIGNATURE_ALGORITHM,
        "key_id": TEST_KEY_ID,
        "issued_at": "2026-07-14T00:00:00Z",
        "expires_at": "2026-07-15T00:00:00Z",
        "payload": expected_manifest(),
        "signature": TEST_EXPECTED_SIGNATURE,
    }


def _release_identity(record_type, *, release_sequence=1):
    return {
        "record_type": record_type,
        "format_version": 1,
        "release_channel": authority.RELEASE_CHANNEL,
        "release_sequence": release_sequence,
        "distribution_version": "0.1.0",
        "release_nonce": "a" * 64,
        "commit": "b" * 40,
        "distribution_sha256": "sha256:" + "1" * 64,
        "sdist_sha256": "sha256:" + "2" * 64,
        "installed_tree_digest": "sha256:" + "3" * 64,
        "image_set_digest": "sha256:" + "4" * 64,
        "policy_digest": "sha256:" + "5" * 64,
        "corpus_digest": "sha256:" + "6" * 64,
        "expected_manifest_digest": "sha256:" + "7" * 64,
        "production_aggregate_digest": "sha256:" + "8" * 64,
    }


def signed_candidate_envelope():
    payload = _release_identity("docker_sandbox_candidate_attestation")
    payload["candidate_nonce"] = "c" * 64
    return {
        "record_type": "pico_signed_release_envelope",
        "format_version": 1,
        "purpose": authority.CANDIDATE_ATTESTATION_PURPOSE,
        "algorithm": authority.SIGNATURE_ALGORITHM,
        "key_id": TEST_KEY_ID,
        "issued_at": "2026-07-14T00:00:00Z",
        "expires_at": "2026-07-15T00:00:00Z",
        "payload": payload,
        "signature": TEST_CANDIDATE_SIGNATURE,
    }


def signed_chain_candidate_envelope():
    payload = _release_identity("docker_sandbox_candidate_attestation")
    payload.update(
        {
            "candidate_nonce": "c" * 64,
            "sdist_sha256": SDIST_DIGEST,
            "installed_tree_digest": "sha256:" + "5" * 64,
            "image_set_digest": "sha256:" + "f" * 64,
            "policy_digest": "sha256:" + "4" * 64,
            "corpus_digest": release.CORPUS_DIGEST,
            "expected_manifest_digest": release.expected_manifest_digest(
                expected_manifest()
            ),
            "production_aggregate_digest": TEST_CHAIN_PRODUCTION_AGGREGATE_DIGEST,
        }
    )
    return {
        "record_type": "pico_signed_release_envelope",
        "format_version": 1,
        "purpose": authority.CANDIDATE_ATTESTATION_PURPOSE,
        "algorithm": authority.SIGNATURE_ALGORITHM,
        "key_id": TEST_KEY_ID,
        "issued_at": "2026-07-14T00:00:00Z",
        "expires_at": "2026-07-15T00:00:00Z",
        "payload": payload,
        "signature": TEST_CHAIN_CANDIDATE_SIGNATURE,
    }


def candidate_smoke_expected_manifest():
    expected = expected_manifest()
    return {
        "record_type": "docker_sandbox_candidate_smoke_expected",
        "format_version": 1,
        "release_nonce": expected["release_nonce"],
        "candidate_nonce": "c" * 64,
        "commit": expected["commit"],
        "distribution_sha256": expected["distribution_sha256"],
        "sdist_sha256": expected["sdist_sha256"],
        "image_set_digest": expected["image_set_digest"],
        "images": expected["images"],
        "policy_digest": expected["policy_digest"],
        "corpus_digest": expected["corpus_digest"],
        "production_aggregate_digest": TEST_CHAIN_PRODUCTION_AGGREGATE_DIGEST,
        "candidate_attestation_digest": TEST_CHAIN_CANDIDATE_DIGEST,
        "jobs": [dict(job) for job in release.expected_candidate_smoke_jobs()],
    }


def signed_candidate_smoke_expected_envelope():
    return {
        "record_type": "pico_signed_release_envelope",
        "format_version": 1,
        "purpose": authority.CANDIDATE_SMOKE_EXPECTED_PURPOSE,
        "algorithm": authority.SIGNATURE_ALGORITHM,
        "key_id": TEST_KEY_ID,
        "issued_at": "2026-07-14T00:00:00Z",
        "expires_at": "2026-07-15T00:00:00Z",
        "payload": candidate_smoke_expected_manifest(),
        "signature": TEST_SMOKE_EXPECTED_SIGNATURE,
    }


def signed_product_envelope(*, release_sequence=1):
    payload = _release_identity(
        "docker_sandbox_product_enablement",
        release_sequence=release_sequence,
    )
    payload.update(
        {
            "candidate_attestation_digest": "sha256:" + "9" * 64,
            "smoke_expected_manifest_digest": "sha256:" + "a" * 64,
            "candidate_smoke_aggregate_digest": "sha256:" + "b" * 64,
        }
    )
    return {
        "record_type": "pico_signed_release_envelope",
        "format_version": 1,
        "purpose": authority.PRODUCT_ENABLEMENT_PURPOSE,
        "algorithm": authority.SIGNATURE_ALGORITHM,
        "key_id": TEST_KEY_ID,
        "issued_at": "2026-07-14T00:00:00Z",
        "expires_at": "2027-07-14T00:00:00Z",
        "payload": payload,
        "signature": (
            TEST_PRODUCT_SIGNATURE
            if release_sequence == 1
            else TEST_PRODUCT_SEQUENCE_2_SIGNATURE
        ),
    }


def configure_test_authority(monkeypatch):
    monkeypatch.setattr(authority, "TRUSTED_RELEASE_KEYS", TEST_KEYS)
    monkeypatch.setattr(authority, "_utc_now", lambda: TEST_NOW)
