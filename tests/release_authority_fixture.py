"""Non-production release key and signed expected manifest for tests."""

from datetime import datetime, timezone
from types import MappingProxyType

from pico import sandbox_release_authority as authority
from pico import docker_sandbox_network_control as network_control
from scripts import docker_sandbox_release as release


TEST_KEY_ID = "test-release-2026"
TEST_MODULUS = "1HOs2apbqWN6GAq-7pXbPHJAltP6RZl6Tg0qV_pgvMYIuKAvN67pBPjQ800GmtpbPdyy0UNkFFHKuycluuMD7Ag8qYG8I71ke4OwcBnGChA6tJWA1gG1oAoCdk7NvtwW1h-VWsETduYwn4NnKQcOOcF6Ff1xacj_phify3dshqiExm2tRbMFvUMEYkVIB4kHjmj1xzlaOc-t_tANOG9vDn8JhkeglMJTkjlhtoet5HcV3cVKwSPk8oJg99bBXnw9KOBxZXnfezMm0CuXAwX1HPo-NrIfFt0LswXMcXiWBuzIMJ3oEVrlr3RE-miekc4kN9gR1hm87vNoM8S6mHXZ87ou6Ct22FnSnXVrYR0IFVF512fhQBdgCIyIZ7rM1g-GLKpDqOa5QT0ATHmnd-M7r5r3SFwmjRTOpDNe6sfdV34VdOqxlRczC2qPG2WNd23U2LuHPm3PipUfrlbDeXF9yN9f5G5kw27Frc-G8QMXAfT3157S3f4oAxZ7gCTnuG9Z"
TEST_EXPECTED_SIGNATURE = "GyawibSBrVAj1W0wVfM9OxfL_PEHZRwpA0n00WABP_8xYClQIyCAInLokibJBsyz6h0WpGQ_u0HrVKCR_7iq3MH6tHAx90bDzkmI76UhKdW9PVbK-ndyyRIpVIg7NMhCiq_WkeRh1nr66syIGAhiRYzjBGWSEV1SwQJp91vsj_i7UNTe30fPyTOc4dNIvbsv67IUvPUqcs5BJNA00Lw8YEdqchOHxoXsUQhxMsAxj-2zP9-fELFKxM2Bx0aG03uaiNIZyAgU31jM1JKPZIyrjpSrAZzdHtQL5BiIh9O_ByKUrT6qvIJsNau29iiIhCxzYDTNVfpVBx6vHfv94Lm2DebcFZgLCJ9eASM5agTfvirIGMJAX4pmHuu1DiuFTWAPDvKGJtAgBGGqqZQIphwHjp3M3cL9ZKhkuOYu_m_2tSWLQqcdVumsZTD8GTFhODZ6YEeEWq9xgS5_aqu4Ph9mlucUCZgcRNgjAhei8PaKza_H6XFRtcaZzmxnXXqmpJfn"
TEST_CANDIDATE_SIGNATURE = "ywAGgWrLKr05MXl5lOv9JhJUMJioerCNCg4N3eAO3OejqcYVxw48Aod1FLTY_Lz3X3amHR2uECD6RXnH4JCedWVbG4fSZ44oJvoM6KE5okKrNMau7rqy8G3b-KhVZZPoCI8w-PyMVvUPpQmml5bb6lTXEUR-hq5Z81iXczibKxgSyvjsqIICqQQ3Y69dKctqXDVpOW5HAnntx670xrbewONefXmmogTKS3CQULRtzkb2VGuJVNE9BBHaCD52LSmwrva8s5naYXl3Sg8-cpO_8g7-ts7virMXgRDuTOLTcMOBVz3zA_UhAF4PDlk8IcJQXQXZd2Ik7s6DPBxxOdaDhcuo9RwSsOsxjW_e_SBX73r6YPCXNuuEMquJ1Z8v54VQ2frNZieDIwsGujZweKN472kchrQ-azxyZ4jH7Dy3DKiKgVJpnE03uPpg5jDuUOd5Ze8vTKca7ZtKXLG8O3sR9QMWRiw0EK17A8K9uWhlF_55rOIR3naLCKxUYLnlD3AL"
TEST_PRODUCT_SIGNATURE = "UKKPGCv_s-i9Jo8f4uhI0xEaKsp4-MYymXHYNY-Uu0nFUyNkU5QXeBnYOQeuJ4_DiAtvBdQD3bQ5wYsOZpvhJ0joJ1qPH51-M2HDYE-u5WLgK3oKk7RV1Q4Iqk6fr2PsZUStSmV0ExG_E1JEAE4YDurZawyTwk-JR-FydebV_jlbqTmG2xP1K6hjtR0WjRbxSfIDkJwdM1kB96LLZKIa7Vm1NaUBcf86H6Bq068hCIGWngk4COk4jFnfmDr41ET089EwI6Eb-9KuR62pk0hBongZ9m7tM_STSXrJ1ApZzw0nKbUyJP22jV5PO952u1WL0a9qmlY26WvvPOjd8QjOod7rEfMVSfDYIuXKdgAX3Td-Z4S1CVCEd2tjdqcGJnCGNcl90hZLaIoX0JhKeM1pQmg-X1kd2lfWnP26zNzZ0eOPZU39wwy-D3WcIhalXMTQ5kKKvVJVrQFr1_weMQxDVfT9jL-V4yye8Z9cljq_rE0nUFtQ1T2QQerYuekRCFvg"
TEST_PRODUCT_SEQUENCE_2_SIGNATURE = "lNVztjxjN4ALmIAU4svrpSX4DX2mTHh4tRdkWth1_yqaHoXFoV1tLHgEVIkgaso9epAwSTlqRuIH3T8ERcU6LVIZ9qTo24Z7rgiHRB0Cf6hPycm3t1Pw0XK9L2-y5l4-0LGdctWN-So1gYGs062As0uK85iIyRBRHXD7sOp4fC4ofZW2GkIYGzedx6zWpfTUtffHd9UJ2kbO-eSGydij5yRYw2RHRiIvk-qrWlFD_crP_4NIXDjQbmX2YxjEIni1D9otGxviKbO6oTbuZZA2uJobIzz9emtEHdYIU8NRtzjcPkuo5onQHC4UR5nQON4BZimOMi6cEUlygxX2QAEvMJVOGAIYRtahtvm7fwhYMkl218dBtjVnT7e3prNvwK2d7osDdg_03nOgwdmh-Gv91cCrts8yvAdvcb1hayvtp0QqoRExoFk6MnAwM5yonC9onpr4Byoq_1fIcZvvhPiQjwxbwb-9M376ExkqPqANIlCaz7kW2v2txB7hJuBkFQnR"
TEST_CHAIN_PRODUCTION_AGGREGATE_DIGEST = (
    "sha256:fde7f438588ca1a90af293edc312f359340cecba02efdf154eb3c766beee0939"
)
TEST_CHAIN_CANDIDATE_DIGEST = (
    "sha256:e4daacf0296538c93fbd8861d27239af35e64577626c3f39cd13030d2bc78148"
)
TEST_CHAIN_CANDIDATE_SIGNATURE = "Pl6W6z7m8wRmqvofv71GAO62u_N2S4EnHPi8YDARRxrM9cxHUWykssmGpR8wWfwpo_W9mi7prReePd46Vq1qe9IDk8-lxINbFhtVcziOcTmdDDUScHGxwFvHsm4N1oZlEMoepgt9PZT6gwyxRNh80pP8GhETlpXXuao6b4MWnUmb776-TseOnMPVxzippcfhqLiAMa0sjIrz1KCY9i88lCbvmU_uO3e9SJtmZ9T-FG1dckWeK2A3TBP8LEolDWS-C7WcMpLVcMs0PohQ7nb32a5ge-q0ggDnLgYuijLitNEiHxoj2PrTRVZfK4Jk5aCXNfQjgmsS1DVssjwlXYuoJdP0-V60QFPNzfkknP_2tjfLyl7iWaQBwcN-guHPUTgEll8mI2Eyhx8O32GUJH8kuZ8oY3hlXH9IX3ksBrpA7WIHJGg2HkfA269gkh_jLqHR1GKCrDgbVkR_SETtq25EfD-6t7dr_2c0rmZkFBAYwZcgk8epg_NmmOeAcwdz5wS4"
TEST_SMOKE_EXPECTED_SIGNATURE = "e8OZrzFI6s_1SweNpTY_QPaGBeLNKM99WhcZCf7cW1SognUvds7_1V7RlnnZ6n_benl5FXDKrOpBplu9BaMeC0vHFdtTLsKwChVo3l_RsVQCWEs-t6qMN2xW7G5henMi9P04e92rG4dNNpHxxrjgcsg-3xRTwwF6IfsN-SQOTxxRR0rYr-6twu6C01jHaUhO7jHGk12PDWITKnFgmUC3vVszMKPfl_aVb-cw3P0Q5OAJuiD8GhUG_010h23IaD2saOf0N_uBzFaenoVEfSz8Tc7yOsqMIhvI65KalD_RoWYY0uuZOYmu_hfybBLdVJ7CN3E0kprE4Iv7hsUXz3wfFH3_ML19xfGRTLMa4PqaveOEql0Ua6fPdMzWkfdxCq0F467E7eTe6KS1JlF_Jn4dYjcPrGdw54XSuIU0T6d57duDemMbYLVNIUZop1ez61GktmgZIEsb-eFBQ0jYctk314Kiy6OJh8Pl-JhfkiWur1fsuTvIWGQBvUYnUFoxr07P"
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
