"""Non-production release key and signed expected manifest for tests."""

from datetime import datetime, timezone
from types import MappingProxyType

from pico import sandbox_release_authority as authority
from pico import docker_sandbox_network_control as network_control
from scripts import docker_sandbox_release as release


TEST_KEY_ID = "test-release-2026"
TEST_MODULUS = "ylHhdFTtt42JsZ1FNdLlnn0yabFRZTvW4-qWW0Nn9usy5Dw19wJb-h-e1FMJzhJK36LgN_XQbvS9vYjKw69SfzInqt-ZZjhkzmnWJK3yrO_9OPqi7jE8J0VEueWgefAHnNQcX3-cZnF19uAIYtfcge-wg00t1DZ-IJsHoxmQQ-Rt_tsmBgzwpPI4UOV0Kla27iOQqdHr1x_JY8nXo2OdgA4019cPdsri3esYojhOVpv8SPIBYBUcAPIw-JvkdLKItHgvFpIjUAb-HE4FAu3g3kSJGDOwUH0c7LIazytfpU1E3I1EtftiQU6SuqDzlOy1Ess5ldikLvontVanEF0FPb4whv1fVtUeRL56bvvKOEiy-MDg2GlhO3c7DopKK7yrT66ZpEibLJiDEtLVZgTUTc4ZpjkpDhW2C5RaaSU8_wobn8aZ2LJ-iaVY8bKA_evfNCx2TPyOp3Imxk5SnSCjcCErMHy7aJ3t4ngwxnKsWKoUEyjhHgRvBgwSBNyPRilZ"
TEST_EXPECTED_SIGNATURE = "ApOH56Qec6agBT1zPFMU9DtuJ_kY9lt-yn8HeOMqEj1B5mmCd3j6iEpoq4-pM0dGa1GvGTPhHrQefs1HVbBFNaoV3QNqOQBW-uZSY16OVkTPvoQ5EHV_FCW_BfH7vT9hnAxjc4ABRaWpiX626RbW1Q1-yqBo5Z3i-D3AFTVYrNIaACGYP1gQ1I5yDqz-r-38t0W1P0twpveVzSo_irEQUyRP82GXApdcEYaXO6bh0gl6VdwhvESXoafZNVoD-zoGh78At0yTwmsPVLIVTVDo0A1tb_cg34kX0gFS94BVlbmdZGvyLD9FJKeidoh1O5qd_sK8F9RBQ12WhpkdENQhwtWHaGFPkbUFvWxD4mILgG1kLIuRMzBh6vjWEyZauGa_0lYcDugpdDkY8ubKobmvi0pQyFai6g3VHyExjSFtJwcckahguLP1yynar-ozXqiTp4cZJ6czjUiufH7XBaV0hK7rUHlN7TFCppjFDytdpuEiW3p6eP2HqTsrzVMH3ELS"
TEST_CANDIDATE_SIGNATURE = "qWKAz5xN-zIphWlnc0dNXk9ktJirmTKqVqBvkWVWTH7YYb72Vc82Pko4z8bMbtZf1lBusD3W05o6o9tQYPLV-8zUFr1My4jesdKCHm26nHJECcE3xgBh5oulrbNxWPjte_cCwcHpH3Fl8pJgjux397Hty4ZmXSA0ylA4FbSOB91u1lPCaqDcLcnCC6yJ2VN3UPf9GaEbh7eNYVWCWvsVr5YDU8tPi9gxJ8LXMwa3blvX33t6HLemksOZx8ax3ebsNAiajuo9QgeYtkPeAT2qXMTVQUkNUWLZ0QuJGiC-MCPjRXb62KM0IvQxak86H7PmcigwmTrCqC0nWK_Vjpgt53k0REMPfFrLUYNbYT02C70cZPJTLHyAQdPoq-7oH9YDnmLqtlVz25p2-s3OzXtOm7EFrX0SDCcjtYh9zpZOgoCLzwK8SCjaJ_xtlT6NP9XcHQb-t3DAKDnkVxQ6X_WTDccuskqz_eAljCsGuVIKX9tquS7C-sZX5nAvZkX3KG3f"
TEST_PRODUCT_SIGNATURE = "tRWsNtHZ-PxuSU_gsxexE7C7QtG5Zj5Rw9XjJNg8vNqlJ-0JOjequNGcF_NOz2618CMexs642caYI136yw0GFDCIgxtn58ACvEa0pXxVGsPkDAZ04RgdISrLTXNdUnpf7-JyaEp3s9ebWJqas9OOnYvDiJelqKjgSA-uVtaCHVi2shCUtIyhCdiwohhUxo4mqsX_e5roDUOd_n1ZknFJiatacmefJaOjPIvzBnYZ3qj5ZlYdgCeWVCheKKlfXnrjpnxtpnak--bm0mpNkSD4uiyjgnPR1-GovwFSxcCWVmWM6k00WcBhoKoL_GUyMkuG5Q-29hfKdmX2YQHYis7HGIGPdE7dIwcShRQE7rc6Fhy9HF1aSHqfBqVKbzG-IiiQ4RBdHwV6nXnR_GrZ9zR6HmN-wLNqhEJTgN7N5nnN4bcva_DvosUZz93HFCu54Lx44fsNLJ6CdxsACjG1rE2Aoq6favbtVZGm50IazzlOFqKFZNRwd2x-V2rLyl3-zGIX"
TEST_PRODUCT_SEQUENCE_2_SIGNATURE = "SYmUKh5QxlHdXhGZ744MInFs0lPt7JMbFFv_dmOaXRzRMI4MZTZ0bo6FxCFGrCbEqBTik2WlKoQxa1g0qhxT8dQh0MuOqa9mWv1xgeoruez0MsNWzkPGL83AiDQP24q5yRj_stykmmwDUyYnczllE-eswyO3VBwaljFWEktnHPqvqOVlijGKZcimJe0cNSXFGQlNnpUYWbJZjB0V1XvLRIOQ5WSfBjYlTKv7szVbjLZMfgQ2QywIq_9D00McwHvnoLhZ0ZhEIZ0VM_-isnPbXkASvfawaQ0cnEPdR_ckr0abdeEhuyVFKwAFx7UC7a0s7XaG1WpfQLwQgN0Rq4jvrlbXCXPcEHrQJlQNN5-0xUiNHpUWPiJ1thRzh_1Q7P53iE59B4_k1CExkz_awdj5h1MHybW_6EY1F28xLdRwXJEL8IaVsn42MjnuQuD0-u9LDqSebq0_AQGE_dTN-heW1JHuK9WOBqlR-ytKlO-g8A0NZsj0dx3XGVx47OH9WGg5"
TEST_CHAIN_PRODUCTION_AGGREGATE_DIGEST = (
    "sha256:caa5a551f2cd439bdc63a1427ec29e9de8b6ded7f979298ef2c54b80891edcc5"
)
TEST_CHAIN_CANDIDATE_DIGEST = (
    "sha256:1f54abcf314249bae03d3094f98bb96b633c80e8e3299d4739d638d044c57af4"
)
TEST_CHAIN_CANDIDATE_SIGNATURE = "ZVBs3uccXo-wBRpewKnLq9vQ3-4YivJo3YGg2TsszrUrDxgEbTYujVn3M6PkhaZqInc2e1Y3H7rw8jnJ9SdRrIrhIGcx5Do9y_NHUO54ZRuKLPb5PzMIKd9qwxyhT2wst0Gw3WHsCjzlVrXt_PJA_Fe4TGWWIRjvo_WlYPrHjNFSHCdU49pxuFI9gv-dkoHRdijFzxejfhM2LP7xk2uIHvP45lmIay1aAa5N30aYAxTKW3OflcSUNLbYAUadHyW5y-t_7tg4d0w9f1nPz6cgOGVRxlEGKhdw4GgBgpYwFNSc_CL28L9lovt5Uw0hDmI0TB5K3z5f8UIuG08cr0txX0OtBScXYmHPxNI5_Fw6QXQDDdAkR-zJgRGJJHkF4lM25ASLvVm5hYsNoNfEQW0DrJBuW3hibpeDWXRkrcFje-ptVp2HSAaxORZXr7UKNR9jyH_v1nXeCAW-h9WoHlKEzbrLRL26G8P6TxTHB7netfbTeYnzABF1BEvIr1sNBQeX"
TEST_SMOKE_EXPECTED_SIGNATURE = "QSs5MV_c6QxfpUBjX_FsccRGIMlnZCI_dg22War69FHJXBDFUnC0ZuiwIBGHL6cCFuibn8NttmKN97hz5Kh0c6l_tgxU-yP6BoKFoSwQqysGqFs8ihLVC52zODG0zX5wcZIyOugR8iVgx6K-3fJjhCaw3TMSLimjMDbGnw5xkGo6BZDOhEE-xQK3BTA0EPjO2DxdloIXhA6i2gqxfNA9F75NgK6aRi4xuvLQDJQRYFqnkvGs_kwnfOatdE_-QhX8qXglLCfZSyXztrvn_EPSYXbFwiCCW4sE4eWR0aFanecPuKjIDnJ05UV3S0myirD27NLdbEg-aD64SFThWilMUHl2yIyINY2iKmlCe2o0PH72UfPvlJYlDiKPiuesxb3XsveXhUxire-i8TOr6-myLnOm6Ip4MmAwvncc-TJdbj7aPBlyQx6PwktsWtTRQIaZd-UkirbtqnNiCIFgb39LW6GfSA1BJFT8l7WaF3AXVR1mVJyi1uXMMM6exG99WWBS"
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
