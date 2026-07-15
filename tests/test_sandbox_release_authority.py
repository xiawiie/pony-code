import base64
from copy import deepcopy
from datetime import timedelta
import hashlib
from pathlib import Path
import subprocess
import stat
import sys
from types import MappingProxyType
from types import SimpleNamespace
import venv
import zipfile

import pytest

from pico import sandbox_release_authority as authority
from tests.release_authority_fixture import (
    configure_test_authority,
    signed_candidate_envelope,
    signed_expected_envelope,
    signed_product_envelope,
    TEST_KEYS,
    TEST_NOW,
)


@pytest.fixture(autouse=True)
def _trusted_test_release_key(monkeypatch):
    configure_test_authority(monkeypatch)


def test_signed_expected_manifest_uses_domain_separated_rsa_pss():
    envelope = signed_expected_envelope()

    payload = authority.verify_signed_envelope(
        envelope,
        purpose=authority.EXPECTED_MANIFEST_PURPOSE,
    )

    assert payload == envelope["payload"]
    assert authority.signing_message(envelope).startswith(authority.SIGNATURE_DOMAIN)
    assert authority.canonical_digest(envelope).startswith("sha256:")


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        (
            lambda value: value["payload"].__setitem__("commit", "c" * 40),
            "release_signature_invalid",
        ),
        (
            lambda value: value.__setitem__("purpose", "docker_sandbox_product_enablement"),
            "release_attestation_invalid",
        ),
        (
            lambda value: value.__setitem__("signature", value["signature"][:-1] + "A"),
            "release_signature_invalid",
        ),
        (
            lambda value: value.__setitem__("unexpected", True),
            "release_attestation_invalid",
        ),
    ),
)
def test_signed_envelope_rejects_tampering_or_schema_drift(mutation, code):
    baseline = signed_expected_envelope()
    assert authority.verify_signed_envelope(
        baseline,
        purpose=authority.EXPECTED_MANIFEST_PURPOSE,
    ) == baseline["payload"]
    envelope = deepcopy(baseline)
    mutation(envelope)

    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.verify_signed_envelope(
            envelope,
            purpose=authority.EXPECTED_MANIFEST_PURPOSE,
        )

    assert caught.value.code == code


def test_release_authority_is_fail_closed_when_unconfigured_revoked_or_expired(
    monkeypatch,
):
    envelope = signed_expected_envelope()
    monkeypatch.setattr(authority, "TRUSTED_RELEASE_KEYS", MappingProxyType({}))
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.verify_signed_envelope(
            envelope,
            purpose=authority.EXPECTED_MANIFEST_PURPOSE,
        )
    assert caught.value.code == "release_authority_unconfigured"

    revoked = {key: dict(value) for key, value in TEST_KEYS.items()}
    revoked[next(iter(revoked))]["status"] = "revoked"
    monkeypatch.setattr(authority, "TRUSTED_RELEASE_KEYS", MappingProxyType(revoked))
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.verify_signed_envelope(
            envelope,
            purpose=authority.EXPECTED_MANIFEST_PURPOSE,
        )
    assert caught.value.code == "release_signing_key_revoked"

    monkeypatch.setattr(authority, "TRUSTED_RELEASE_KEYS", TEST_KEYS)
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.verify_signed_envelope(
            envelope,
            purpose=authority.EXPECTED_MANIFEST_PURPOSE,
            now=TEST_NOW + timedelta(days=2),
        )
    assert caught.value.code == "release_attestation_expired"


def _release_identity(record_type):
    value = {
        "record_type": record_type,
        "format_version": 1,
        "release_channel": authority.RELEASE_CHANNEL,
        "release_sequence": 1,
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
    return value


def test_candidate_and_product_payloads_are_exact_and_purpose_specific():
    candidate = _release_identity("docker_sandbox_candidate_attestation")
    candidate["candidate_nonce"] = "c" * 64
    assert authority.validate_candidate_attestation_payload(candidate) is candidate

    product = _release_identity("docker_sandbox_product_enablement")
    product.update(
        {
            "candidate_attestation_digest": "sha256:" + "9" * 64,
            "smoke_expected_manifest_digest": "sha256:" + "a" * 64,
            "candidate_smoke_aggregate_digest": "sha256:" + "b" * 64,
        }
    )
    assert authority.validate_product_enablement_payload(product) is product

    candidate["product_enablement"] = True
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.validate_candidate_attestation_payload(candidate)
    assert caught.value.code == "release_attestation_invalid"


def test_installed_tree_digest_is_stable_and_rejects_unsupported_entries(tmp_path):
    package = tmp_path / "pico"
    package.mkdir()
    (package / "module.py").write_text("value = 1\n", encoding="utf-8")
    data = package / "data.json"
    data.write_text("{}\n", encoding="utf-8")
    data.chmod(0o644)

    first = authority.installed_tree_digest(package)
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "module.pyc").write_bytes(b"ignored")
    assert authority.installed_tree_digest(package) == first

    data.write_text('{"changed":true}\n', encoding="utf-8")
    assert authority.installed_tree_digest(package) != first

    (package / "linked").symlink_to("module.py")
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.installed_tree_digest(package)
    assert caught.value.code == "installed_distribution_invalid"


def test_installed_tree_only_ignores_generated_cache_bytecode(tmp_path):
    package = tmp_path / "pico"
    cache = package / "__pycache__"
    cache.mkdir(parents=True)
    (package / "module.py").write_text("value = 1\n", encoding="utf-8")
    first = authority.installed_tree_digest(package)

    (cache / "module.pyc").write_bytes(b"generated")
    assert authority.installed_tree_digest(package) == first

    (cache / "payload.so").write_bytes(b"not bytecode")
    assert authority.installed_tree_digest(package) != first
    (cache / "payload.so").unlink()

    (package / "payload.pyc").write_bytes(b"root bytecode is not generated cache")
    assert authority.installed_tree_digest(package) != first


def _write_installed_distribution(root, version="0.1.0"):
    package = root / "pico"
    package.mkdir()
    module = package / "module.py"
    module.write_text("value = 1\n", encoding="utf-8")
    dist_info = root / f"pico-{version}.dist-info"
    dist_info.mkdir()
    files = {
        module: "pico/module.py",
        dist_info / "METADATA": f"pico-{version}.dist-info/METADATA",
        dist_info / "WHEEL": f"pico-{version}.dist-info/WHEEL",
        dist_info / "entry_points.txt": f"pico-{version}.dist-info/entry_points.txt",
        dist_info / "top_level.txt": f"pico-{version}.dist-info/top_level.txt",
    }
    for path in files:
        if path != module:
            path.write_text(path.name + "\n", encoding="utf-8")
    rows = []
    for path, relative in files.items():
        raw = path.read_bytes()
        encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        rows.append(
            f"{relative},sha256={encoded.rstrip(b'=').decode('ascii')},{len(raw)}"
        )
    rows.append(f"pico-{version}.dist-info/RECORD,,")
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return package, dist_info


def test_installed_distribution_digest_binds_record_and_dist_info(tmp_path):
    package, dist_info = _write_installed_distribution(tmp_path)

    digest = authority.installed_tree_digest(package, "0.1.0")

    assert digest.startswith("sha256:")
    (dist_info / "METADATA").write_text("changed\n", encoding="utf-8")
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.installed_tree_digest(package, "0.1.0")
    assert caught.value.code == "installed_distribution_invalid"


def _write_test_wheel(root):
    wheel = root / "pico-0.1.0-py3-none-any.whl"
    dist_info = "pico-0.1.0.dist-info"
    files = {
        "pico/__init__.py": b"def main():\n    return None\n",
        f"{dist_info}/METADATA": (
            b"Metadata-Version: 2.1\nName: pico\nVersion: 0.1.0\n"
        ),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": b"[console_scripts]\npico = pico:main\n",
        f"{dist_info}/top_level.txt": b"pico\n",
    }
    rows = []
    for relative, raw in files.items():
        encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        rows.append(
            f"{relative},sha256={encoded.rstrip(b'=').decode('ascii')},{len(raw)}"
        )
    rows.append(f"{dist_info}/RECORD,,")
    with zipfile.ZipFile(wheel, "w") as archive:
        for relative, raw in files.items():
            archive.writestr(relative, raw)
        archive.writestr(f"{dist_info}/RECORD", "\n".join(rows) + "\n")
    return wheel


def test_installed_distribution_accepts_a_real_pip_console_script(tmp_path):
    wheel = _write_test_wheel(tmp_path)
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-index",
            str(wheel),
        ],
        check=True,
        capture_output=True,
    )
    installed = subprocess.run(
        [
            str(python),
            "-c",
            "from pathlib import Path; import pico; "
            "root=Path(pico.__file__).resolve().parent; "
            "record=root.parent/'pico-0.1.0.dist-info'/'RECORD'; "
            "print(root); print(record.read_text())",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    ).stdout.splitlines()
    package = installed[0]

    assert any(line.startswith("../../../bin/pico,") for line in installed[1:])
    assert any("pico/__pycache__/" in line for line in installed[1:])
    assert authority.installed_tree_digest(package, "0.1.0").startswith("sha256:")


def test_installed_distribution_rejects_unhashed_non_cache_package_record(tmp_path):
    package, dist_info = _write_installed_distribution(tmp_path)
    with (dist_info / "RECORD").open("a", encoding="utf-8") as record:
        record.write("pico/unhashed.py,,\n")

    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.installed_tree_digest(package, "0.1.0")

    assert caught.value.code == "installed_distribution_invalid"


@pytest.mark.parametrize(
    "path",
    (
        "/bin/pico",
        "..",
        "../../../bin/../pico",
        "../../../bin\\pico",
        "../../../bin/pico\0suffix",
        "../../../bin/pico\x7fsuffix",
        "pico//module.py",
        "pico/./module.py",
    ),
)
def test_installed_record_rejects_unsafe_paths(path):
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority._installed_record_rows(f"{path},,\n".encode())

    assert caught.value.code == "installed_distribution_invalid"


def test_installed_record_rejects_duplicate_external_path():
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority._installed_record_rows(
            b"../../../bin/pico,,\n../../../bin/pico,,\n"
        )

    assert caught.value.code == "installed_distribution_invalid"


def test_installed_record_accepts_standard_external_console_script():
    assert authority._installed_record_rows(b"../../../bin/pico,,\n") == {
        "../../../bin/pico": ("", "")
    }


@pytest.mark.parametrize("relative", ("__pycache__/payload.so", "payload.pyc"))
def test_installed_distribution_does_not_ignore_other_cache_like_files(
    tmp_path,
    relative,
):
    package, _dist_info = _write_installed_distribution(tmp_path)
    original = authority.installed_tree_digest(package, "0.1.0")
    path = package / relative
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"payload")

    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.installed_tree_digest(package, "0.1.0")

    assert caught.value.code == "installed_distribution_invalid"
    path.unlink()
    assert authority.installed_tree_digest(package, "0.1.0") == original


def _release_image():
    return SimpleNamespace(
        image_set_digest="sha256:" + "4" * 64,
        policy_digest="sha256:" + "5" * 64,
        corpus_digest="sha256:" + "6" * 64,
        registry_reference="registry.example/pico@sha256:" + "d" * 64,
        reference="sha256:" + "d" * 64,
    )


def test_candidate_is_smoke_only_and_nonce_bound(monkeypatch, tmp_path):
    monkeypatch.setattr(
        authority,
        "installed_tree_digest",
        lambda _root, _version=None: "sha256:" + "3" * 64,
    )
    envelope = signed_candidate_envelope()

    payload = authority.verify_candidate_attestation(
        envelope,
        package_root=tmp_path,
        distribution_version="0.1.0",
        image=_release_image(),
        candidate_nonce="c" * 64,
    )

    assert payload["record_type"] == "docker_sandbox_candidate_attestation"
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.verify_candidate_attestation(
            envelope,
            package_root=tmp_path,
            distribution_version="0.1.0",
            image=_release_image(),
            candidate_nonce="d" * 64,
        )
    assert caught.value.code == "sandbox_candidate_attestation_mismatch"


def test_product_enablement_cache_is_read_only_exact_and_rollback_safe(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        authority,
        "installed_tree_digest",
        lambda _root, _version=None: "sha256:" + "3" * 64,
    )
    cache_root = tmp_path / "release-cache"
    image = _release_image()
    arguments = {
        "package_root": tmp_path,
        "distribution_version": "0.1.0",
        "image": image,
        "cache_root": cache_root,
    }

    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.load_cached_product_enablement(**arguments)
    assert caught.value.code == "sandbox_product_not_enabled"
    assert not cache_root.exists()

    first = signed_product_envelope()
    payload = authority.cache_product_enablement(
        authority.canonical_json(first),
        **arguments,
    )
    cache_path = cache_root / authority.PRODUCT_ENABLEMENT_CACHE_NAME
    assert payload["release_sequence"] == 1
    assert stat.S_IMODE(cache_path.lstat().st_mode) == 0o600
    assert not (cache_root / ".product-enablement.lock").exists()
    before = cache_path.lstat()
    assert authority.load_cached_product_enablement(**arguments) == payload
    after = cache_path.lstat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)

    second = signed_product_envelope(release_sequence=2)
    assert authority.cache_product_enablement(
        authority.canonical_json(second),
        **arguments,
    )["release_sequence"] == 2
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.cache_product_enablement(
            authority.canonical_json(first),
            **arguments,
        )
    assert caught.value.code == "sandbox_product_enablement_rollback"

    real_verify = authority.verify_signed_envelope
    future = TEST_NOW + timedelta(days=400)

    def verify_with_expired_current(envelope, *, purpose, now=None):
        if envelope == first and now == future:
            now = TEST_NOW
        elif envelope == second and now == future:
            raise authority.ReleaseAuthorityError("release_attestation_expired")
        return real_verify(envelope, purpose=purpose, now=now)

    monkeypatch.setattr(authority, "verify_signed_envelope", verify_with_expired_current)
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.cache_product_enablement(
            authority.canonical_json(first),
            now=future,
            **arguments,
        )
    assert caught.value.code == "sandbox_product_enablement_rollback"
    monkeypatch.setattr(authority, "verify_signed_envelope", real_verify)

    cache_path.write_text("{}", encoding="utf-8")
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.load_cached_product_enablement(**arguments)
    assert caught.value.code == "sandbox_product_enablement_invalid"
    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.cache_product_enablement(
            authority.canonical_json(second),
            **arguments,
        )
    assert caught.value.code == "sandbox_product_enablement_invalid"


def test_product_enablement_rejects_wrong_installed_or_image_identity(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        authority,
        "installed_tree_digest",
        lambda _root, _version=None: "sha256:" + "0" * 64,
    )

    with pytest.raises(authority.ReleaseAuthorityError) as caught:
        authority.verify_product_enablement(
            signed_product_envelope(),
            package_root=Path(tmp_path),
            distribution_version="0.1.0",
            image=_release_image(),
        )

    assert caught.value.code == "sandbox_product_enablement_mismatch"
