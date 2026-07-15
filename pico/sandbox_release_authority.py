"""Detached release signatures for Docker Sandbox release evidence."""

from __future__ import annotations

import base64
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
from types import MappingProxyType
import re
import ssl
import stat
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from . import file_lock
from . import security as securitylib


SIGNATURE_ALGORITHM = "rsa-pss-sha256"
SIGNATURE_DOMAIN = b"PICO_DOCKER_SANDBOX_RELEASE_V1\0"
RELEASE_CHANNEL = "stable"
MAX_SIGNED_ENVELOPE_BYTES = 256 * 1024
RSA_MODULUS_BITS = 3072
RSA_PUBLIC_EXPONENT = 65537
PSS_SALT_BYTES = 32
CLOCK_SKEW = timedelta(minutes=5)
MINIMUM_PRODUCT_RELEASE_SEQUENCE = 1
MAX_INSTALLED_TREE_BYTES = 512 * 1024 * 1024
MAX_INSTALLED_FILE_BYTES = 256 * 1024 * 1024
MAX_INSTALLED_TREE_ENTRIES = 10_000
PRODUCT_ENABLEMENT_CACHE_NAME = "product-enablement.json"
CANDIDATE_ATTESTATION_ENV = "PICO_SANDBOX_CANDIDATE_ATTESTATION"
CANDIDATE_NONCE_ENV = "PICO_SANDBOX_CANDIDATE_NONCE"
PRODUCT_ENABLEMENT_URL_TEMPLATE = (
    "https://github.com/xiawiie/pico/releases/download/v{version}/"
    "pico-{version}-docker-sandbox-product-enablement.json"
)
PRODUCT_ENABLEMENT_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)

EXPECTED_MANIFEST_PURPOSE = "docker_sandbox_release_expected"
CANDIDATE_ATTESTATION_PURPOSE = "docker_sandbox_candidate_attestation"
CANDIDATE_SMOKE_EXPECTED_PURPOSE = "docker_sandbox_candidate_smoke_expected"
PRODUCT_ENABLEMENT_PURPOSE = "docker_sandbox_product_enablement"
_PURPOSE_MAX_LIFETIME = {
    EXPECTED_MANIFEST_PURPOSE: timedelta(hours=24),
    CANDIDATE_ATTESTATION_PURPOSE: timedelta(hours=24),
    CANDIDATE_SMOKE_EXPECTED_PURPOSE: timedelta(hours=24),
    PRODUCT_ENABLEMENT_PURPOSE: timedelta(days=366),
}

# Production keys are added only by a release-authority change. They must never
# come from the workspace, environment, expected manifest, or signed artifact.
TRUSTED_RELEASE_KEYS = MappingProxyType({})

_ENVELOPE_FIELDS = {
    "record_type",
    "format_version",
    "purpose",
    "algorithm",
    "key_id",
    "issued_at",
    "expires_at",
    "payload",
    "signature",
}
_KEY_FIELDS = {
    "algorithm",
    "modulus",
    "exponent",
    "not_before",
    "not_after",
    "status",
}
_CANDIDATE_FIELDS = {
    "record_type",
    "format_version",
    "release_channel",
    "release_sequence",
    "distribution_version",
    "release_nonce",
    "candidate_nonce",
    "commit",
    "distribution_sha256",
    "sdist_sha256",
    "installed_tree_digest",
    "image_set_digest",
    "policy_digest",
    "corpus_digest",
    "expected_manifest_digest",
    "production_aggregate_digest",
}
_PRODUCT_FIELDS = {
    "record_type",
    "format_version",
    "release_channel",
    "release_sequence",
    "distribution_version",
    "release_nonce",
    "commit",
    "distribution_sha256",
    "sdist_sha256",
    "installed_tree_digest",
    "image_set_digest",
    "policy_digest",
    "corpus_digest",
    "expected_manifest_digest",
    "production_aggregate_digest",
    "candidate_attestation_digest",
    "smoke_expected_manifest_digest",
    "candidate_smoke_aggregate_digest",
}

_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_KEY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_DIST_INFO_FILES = ("METADATA", "WHEEL", "entry_points.txt", "top_level.txt")


class ReleaseAuthorityError(RuntimeError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


def canonical_json(value):
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ReleaseAuthorityError("release_attestation_invalid") from exc


def canonical_digest(value):
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def decode_json(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_SIGNED_ENVELOPE_BYTES:
        raise ReleaseAuthorityError("release_attestation_invalid")

    def reject_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ReleaseAuthorityError("release_attestation_invalid")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ReleaseAuthorityError("release_attestation_invalid")
            ),
        )
    except ReleaseAuthorityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseAuthorityError("release_attestation_invalid") from exc
    if not isinstance(value, dict):
        raise ReleaseAuthorityError("release_attestation_invalid")
    return value


def _timestamp(value):
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise ReleaseAuthorityError("release_attestation_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ReleaseAuthorityError("release_attestation_invalid") from exc


def _utc_now():
    return datetime.now(timezone.utc)


def _base64url(value):
    if not isinstance(value, str) or _BASE64URL_RE.fullmatch(value) is None:
        raise ReleaseAuthorityError("release_attestation_invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error) as exc:
        raise ReleaseAuthorityError("release_attestation_invalid") from exc
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        raise ReleaseAuthorityError("release_attestation_invalid")
    return raw


def _mgf1(seed, length):
    output = bytearray()
    for counter in range((length + hashlib.sha256().digest_size - 1) // hashlib.sha256().digest_size):
        output.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
    return bytes(output[:length])


def _verify_rsa_pss(message, signature, key):
    if not isinstance(key, dict) or set(key) != _KEY_FIELDS:
        raise ReleaseAuthorityError("release_trust_root_invalid")
    if (
        key["algorithm"] != SIGNATURE_ALGORITHM
        or type(key["exponent"]) is not int
        or key["exponent"] != RSA_PUBLIC_EXPONENT
        or key["status"] not in {"active", "revoked"}
    ):
        raise ReleaseAuthorityError("release_trust_root_invalid")
    modulus_raw = _base64url(key["modulus"])
    if not modulus_raw or modulus_raw[0] == 0:
        raise ReleaseAuthorityError("release_trust_root_invalid")
    modulus = int.from_bytes(modulus_raw, "big")
    if modulus.bit_length() != RSA_MODULUS_BITS or modulus % 2 == 0:
        raise ReleaseAuthorityError("release_trust_root_invalid")
    if len(signature) != len(modulus_raw):
        return False
    encoded_signature = int.from_bytes(signature, "big")
    if encoded_signature >= modulus:
        return False
    encoded_length = (modulus.bit_length() - 1 + 7) // 8
    encoded = pow(encoded_signature, key["exponent"], modulus).to_bytes(
        encoded_length,
        "big",
    )
    hash_length = hashlib.sha256().digest_size
    if (
        len(encoded) < hash_length + PSS_SALT_BYTES + 2
        or encoded[-1] != 0xBC
    ):
        return False
    masked_db = encoded[: -hash_length - 1]
    expected_hash = encoded[-hash_length - 1 : -1]
    unused_bits = 8 * encoded_length - (modulus.bit_length() - 1)
    if masked_db[0] & (0xFF << (8 - unused_bits)):
        return False
    mask = _mgf1(expected_hash, len(masked_db))
    database = bytearray(left ^ right for left, right in zip(masked_db, mask))
    database[0] &= 0xFF >> unused_bits
    padding_length = encoded_length - hash_length - PSS_SALT_BYTES - 2
    if (
        any(database[:padding_length])
        or database[padding_length] != 1
        or len(database[padding_length + 1 :]) != PSS_SALT_BYTES
    ):
        return False
    message_hash = hashlib.sha256(message).digest()
    actual_hash = hashlib.sha256(
        b"\0" * 8 + message_hash + bytes(database[-PSS_SALT_BYTES:])
    ).digest()
    return hmac.compare_digest(expected_hash, actual_hash)


def signing_message(envelope):
    if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_FIELDS:
        raise ReleaseAuthorityError("release_attestation_invalid")
    unsigned = {key: value for key, value in envelope.items() if key != "signature"}
    return SIGNATURE_DOMAIN + canonical_json(unsigned)


def verify_signed_envelope(envelope, *, purpose, now=None):
    if purpose not in _PURPOSE_MAX_LIFETIME:
        raise ReleaseAuthorityError("release_attestation_invalid")
    if (
        not isinstance(envelope, dict)
        or set(envelope) != _ENVELOPE_FIELDS
        or envelope.get("record_type") != "pico_signed_release_envelope"
        or type(envelope.get("format_version")) is not int
        or envelope["format_version"] != 1
        or envelope.get("purpose") != purpose
        or envelope.get("algorithm") != SIGNATURE_ALGORITHM
        or not isinstance(envelope.get("key_id"), str)
        or _KEY_ID_RE.fullmatch(envelope["key_id"]) is None
        or not isinstance(envelope.get("payload"), dict)
        or len(canonical_json(envelope)) > MAX_SIGNED_ENVELOPE_BYTES
    ):
        raise ReleaseAuthorityError("release_attestation_invalid")
    issued_at = _timestamp(envelope["issued_at"])
    expires_at = _timestamp(envelope["expires_at"])
    if not issued_at < expires_at or expires_at - issued_at > _PURPOSE_MAX_LIFETIME[purpose]:
        raise ReleaseAuthorityError("release_attestation_invalid")
    if not TRUSTED_RELEASE_KEYS:
        raise ReleaseAuthorityError("release_authority_unconfigured")
    key = TRUSTED_RELEASE_KEYS.get(envelope["key_id"])
    if key is None:
        raise ReleaseAuthorityError("release_signing_key_unknown")
    key_not_before = _timestamp(key.get("not_before") if isinstance(key, dict) else None)
    key_not_after = _timestamp(key.get("not_after") if isinstance(key, dict) else None)
    if not key_not_before < key_not_after:
        raise ReleaseAuthorityError("release_trust_root_invalid")
    if key.get("status") != "active":
        raise ReleaseAuthorityError("release_signing_key_revoked")
    if issued_at < key_not_before or expires_at > key_not_after:
        raise ReleaseAuthorityError("release_signing_key_invalid_for_window")
    signature = _base64url(envelope["signature"])
    if not _verify_rsa_pss(signing_message(envelope), signature, key):
        raise ReleaseAuthorityError("release_signature_invalid")
    current = now or _utc_now()
    if current.tzinfo is None:
        raise ReleaseAuthorityError("release_attestation_invalid")
    current = current.astimezone(timezone.utc)
    if current + CLOCK_SKEW < issued_at:
        raise ReleaseAuthorityError("release_attestation_not_yet_valid")
    if current > expires_at:
        raise ReleaseAuthorityError("release_attestation_expired")
    return envelope["payload"]


def _valid_digest_fields(value, names):
    return all(
        isinstance(value.get(name), str)
        and _SHA256_RE.fullmatch(value[name]) is not None
        for name in names
    )


def _validate_release_identity(value, fields, record_type):
    digest_fields = {
        "distribution_sha256",
        "sdist_sha256",
        "installed_tree_digest",
        "image_set_digest",
        "policy_digest",
        "corpus_digest",
        "expected_manifest_digest",
        "production_aggregate_digest",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("record_type") != record_type
        or type(value.get("format_version")) is not int
        or value["format_version"] != 1
        or value.get("release_channel") != RELEASE_CHANNEL
        or type(value.get("release_sequence")) is not int
        or not 0 < value["release_sequence"] < 2**63
        or not isinstance(value.get("distribution_version"), str)
        or _VERSION_RE.fullmatch(value["distribution_version"]) is None
        or not isinstance(value.get("release_nonce"), str)
        or _NONCE_RE.fullmatch(value["release_nonce"]) is None
        or not isinstance(value.get("commit"), str)
        or _COMMIT_RE.fullmatch(value["commit"]) is None
        or not _valid_digest_fields(value, digest_fields)
    ):
        raise ReleaseAuthorityError("release_attestation_invalid")
    return value


def validate_candidate_attestation_payload(value):
    value = _validate_release_identity(
        value,
        _CANDIDATE_FIELDS,
        "docker_sandbox_candidate_attestation",
    )
    if (
        not isinstance(value.get("candidate_nonce"), str)
        or _NONCE_RE.fullmatch(value["candidate_nonce"]) is None
    ):
        raise ReleaseAuthorityError("release_attestation_invalid")
    return value


def validate_product_enablement_payload(value):
    value = _validate_release_identity(
        value,
        _PRODUCT_FIELDS,
        "docker_sandbox_product_enablement",
    )
    if not _valid_digest_fields(
        value,
        {
            "candidate_attestation_digest",
            "smoke_expected_manifest_digest",
            "candidate_smoke_aggregate_digest",
        },
    ):
        raise ReleaseAuthorityError("release_attestation_invalid")
    return value


def attestation_digest(envelope):
    return canonical_digest(envelope)


def _entry_identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _ignored_installed_path(relative):
    return relative.parent.name == "__pycache__" and relative.suffix in {
        ".pyc",
        ".pyo",
    }


def _installed_inventory(root):
    entries = []
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _ignored_installed_path(relative):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        ):
            raise ReleaseAuthorityError("installed_distribution_invalid")
        entries.append((relative.as_posix(), _entry_identity(info)))
        if len(entries) > MAX_INSTALLED_TREE_ENTRIES:
            raise ReleaseAuthorityError("installed_distribution_invalid")
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1 or info.st_size > MAX_INSTALLED_FILE_BYTES:
                raise ReleaseAuthorityError("installed_distribution_invalid")
            total += info.st_size
            if total > MAX_INSTALLED_TREE_BYTES:
                raise ReleaseAuthorityError("installed_distribution_invalid")
    return entries


def _installed_file_digest(path, expected_identity):
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
            _entry_identity(before) != expected_identity
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            raise ReleaseAuthorityError("installed_distribution_invalid")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ReleaseAuthorityError("installed_distribution_invalid")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ReleaseAuthorityError("installed_distribution_invalid")
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            _entry_identity(after) != expected_identity
            or _entry_identity(current) != expected_identity
        ):
            raise ReleaseAuthorityError("installed_distribution_invalid")
        return "sha256:" + digest.hexdigest()
    except ReleaseAuthorityError:
        raise
    except OSError as exc:
        raise ReleaseAuthorityError("installed_distribution_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _installed_record_rows(raw):
    try:
        rows = {}
        for row in csv.reader(io.StringIO(raw.decode("utf-8"), newline="")):
            if len(row) != 3:
                raise ValueError("invalid RECORD row")
            raw_path = row[0]
            parts = raw_path.split("/")
            external_prefix = next(
                (index for index, part in enumerate(parts) if part != ".."),
                len(parts),
            )
            if (
                "\\" in raw_path
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw_path)
                or external_prefix == len(parts)
                or any(part in {"", "."} for part in parts)
                or any(part == ".." for part in parts[external_prefix:])
            ):
                raise ValueError("invalid RECORD path")
            normalized = PurePosixPath(*parts).as_posix()
            if normalized in rows:
                raise ValueError("duplicate RECORD path")
            rows[normalized] = (row[1], row[2])
        return rows
    except (UnicodeDecodeError, csv.Error, ValueError) as exc:
        raise ReleaseAuthorityError("installed_distribution_invalid") from exc


def _installed_record_bytes(path, identity):
    if identity[6] > MAX_SIGNED_ENVELOPE_BYTES:
        raise ReleaseAuthorityError("installed_distribution_invalid")
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
        if _entry_identity(before) != identity or not stat.S_ISREG(before.st_mode):
            raise ReleaseAuthorityError("installed_distribution_invalid")
        remaining = before.st_size
        chunks = []
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ReleaseAuthorityError("installed_distribution_invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ReleaseAuthorityError("installed_distribution_invalid")
        after = os.fstat(descriptor)
        current = path.lstat()
        if _entry_identity(after) != identity or _entry_identity(current) != identity:
            raise ReleaseAuthorityError("installed_distribution_invalid")
        return b"".join(chunks)
    except ReleaseAuthorityError:
        raise
    except OSError as exc:
        raise ReleaseAuthorityError("installed_distribution_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _record_digest(file_digest):
    raw = bytes.fromhex(file_digest.removeprefix("sha256:"))
    return "sha256=" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def installed_tree_digest(package_root, distribution_version=None):
    root = Path(os.path.abspath(os.fspath(package_root)))
    try:
        root_before = root.lstat()
    except OSError as exc:
        raise ReleaseAuthorityError("installed_distribution_invalid") from exc
    if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
        raise ReleaseAuthorityError("installed_distribution_invalid")
    first = _installed_inventory(root)
    rendered = []
    for relative, identity in first:
        if stat.S_ISDIR(identity[2]):
            continue
        path = root / Path(relative)
        rendered.append(
            {
                "path": relative,
                "mode": stat.S_IMODE(identity[2]),
                "size": identity[6],
                "sha256": _installed_file_digest(path, identity),
            }
        )
    try:
        root_after = root.lstat()
    except OSError as exc:
        raise ReleaseAuthorityError("installed_distribution_invalid") from exc
    if (
        _entry_identity(root_before) != _entry_identity(root_after)
        or first != _installed_inventory(root)
    ):
        raise ReleaseAuthorityError("installed_distribution_invalid")
    if distribution_version is None:
        return canonical_digest(rendered)
    if (
        root.name != "pico"
        or not isinstance(distribution_version, str)
        or _VERSION_RE.fullmatch(distribution_version) is None
    ):
        raise ReleaseAuthorityError("installed_distribution_invalid")
    dist_info = root.parent / f"pico-{distribution_version}.dist-info"
    try:
        dist_before = dist_info.lstat()
    except OSError as exc:
        raise ReleaseAuthorityError("installed_distribution_invalid") from exc
    if stat.S_ISLNK(dist_before.st_mode) or not stat.S_ISDIR(dist_before.st_mode):
        raise ReleaseAuthorityError("installed_distribution_invalid")
    identities = {}
    for name in (*_DIST_INFO_FILES, "RECORD"):
        try:
            info = (dist_info / name).lstat()
        except OSError as exc:
            raise ReleaseAuthorityError("installed_distribution_invalid") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > MAX_INSTALLED_FILE_BYTES
        ):
            raise ReleaseAuthorityError("installed_distribution_invalid")
        identities[name] = _entry_identity(info)
    record_raw = _installed_record_bytes(dist_info / "RECORD", identities["RECORD"])
    records = _installed_record_rows(record_raw)
    package_records = {
        f"pico/{relative}"
        for relative, identity in first
        if stat.S_ISREG(identity[2])
    }
    ignored_package_records = {
        name
        for name, fields in records.items()
        if name.startswith("pico/")
        and _ignored_installed_path(PurePosixPath(name).relative_to("pico"))
        and fields == ("", "")
    }
    dist_records = {f"{dist_info.name}/{name}" for name in _DIST_INFO_FILES}
    record_path = f"{dist_info.name}/RECORD"
    if (
        {name for name in records if name.startswith("pico/")}
        != package_records | ignored_package_records
        or not dist_records <= set(records)
        or records.get(record_path) != ("", "")
    ):
        raise ReleaseAuthorityError("installed_distribution_invalid")
    distribution_files = []
    for name in _DIST_INFO_FILES:
        path = dist_info / name
        identity = identities[name]
        digest = _installed_file_digest(path, identity)
        record_digest, record_size = records[f"{dist_info.name}/{name}"]
        if record_digest != _record_digest(digest) or record_size != str(identity[6]):
            raise ReleaseAuthorityError("installed_distribution_invalid")
        distribution_files.append(
            {
                "path": f"{dist_info.name}/{name}",
                "mode": stat.S_IMODE(identity[2]),
                "size": identity[6],
                "sha256": digest,
            }
        )
    for item in rendered:
        item["path"] = "pico/" + item["path"]
        record_digest, record_size = records[item["path"]]
        if record_digest != _record_digest(item["sha256"]) or record_size != str(
            item["size"]
        ):
            raise ReleaseAuthorityError("installed_distribution_invalid")
    try:
        dist_after = dist_info.lstat()
    except OSError as exc:
        raise ReleaseAuthorityError("installed_distribution_invalid") from exc
    if (
        _entry_identity(dist_before) != _entry_identity(dist_after)
        or any(
            _entry_identity((dist_info / name).lstat()) != identity
            for name, identity in identities.items()
        )
    ):
        raise ReleaseAuthorityError("installed_distribution_invalid")
    return canonical_digest(
        [
            {"distribution": "pico", "version": distribution_version},
            *rendered,
            *distribution_files,
        ]
    )


def _verify_installed_identity(payload, *, package_root, distribution_version, image):
    if (
        payload["release_sequence"] < MINIMUM_PRODUCT_RELEASE_SEQUENCE
        or payload["distribution_version"] != distribution_version
        or payload["installed_tree_digest"]
        != installed_tree_digest(package_root, distribution_version)
        or payload["image_set_digest"] != image.image_set_digest
        or payload["policy_digest"] != image.policy_digest
        or payload["corpus_digest"] != image.corpus_digest
        or not image.registry_reference
        or not image.registry_reference.endswith("@" + image.reference)
    ):
        raise ReleaseAuthorityError("sandbox_product_enablement_mismatch")
    return payload


def verify_candidate_attestation(
    envelope,
    *,
    package_root,
    distribution_version,
    image,
    candidate_nonce,
    now=None,
):
    payload = verify_signed_envelope(
        envelope,
        purpose=CANDIDATE_ATTESTATION_PURPOSE,
        now=now,
    )
    validate_candidate_attestation_payload(payload)
    if payload["candidate_nonce"] != candidate_nonce:
        raise ReleaseAuthorityError("sandbox_candidate_attestation_mismatch")
    return _verify_installed_identity(
        payload,
        package_root=package_root,
        distribution_version=distribution_version,
        image=image,
    )


def verify_product_enablement(
    envelope,
    *,
    package_root,
    distribution_version,
    image,
    now=None,
):
    payload = verify_signed_envelope(
        envelope,
        purpose=PRODUCT_ENABLEMENT_PURPOSE,
        now=now,
    )
    validate_product_enablement_payload(payload)
    return _verify_installed_identity(
        payload,
        package_root=package_root,
        distribution_version=distribution_version,
        image=image,
    )


def product_enablement_cache_root(home=None):
    return Path(home or Path.home()) / ".pico" / "releases" / "docker-sandbox"


def product_enablement_url(distribution_version):
    if (
        not isinstance(distribution_version, str)
        or _VERSION_RE.fullmatch(distribution_version) is None
    ):
        raise ReleaseAuthorityError("sandbox_release_channel_invalid")
    return PRODUCT_ENABLEMENT_URL_TEMPLATE.format(version=distribution_version)


class _ReleaseRedirectHandler(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse.urlsplit(newurl)
        if (
            target.scheme != "https"
            or target.hostname not in PRODUCT_ENABLEMENT_ALLOWED_HOSTS
            or target.username is not None
            or target.password is not None
        ):
            raise ReleaseAuthorityError("sandbox_release_channel_invalid")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_product_enablement(distribution_version, *, opener=None):
    url = product_enablement_url(distribution_version)
    client = opener or urlrequest.build_opener(
        urlrequest.ProxyHandler({}),
        urlrequest.HTTPSHandler(context=ssl.create_default_context()),
        _ReleaseRedirectHandler(),
    )
    request = urlrequest.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "pico-sandbox-release/1"},
        method="GET",
    )
    try:
        with client.open(request, timeout=30) as response:
            final = urlparse.urlsplit(response.geturl())
            content_length = response.headers.get("Content-Length")
            if (
                getattr(response, "status", None) != 200
                or final.scheme != "https"
                or final.hostname not in PRODUCT_ENABLEMENT_ALLOWED_HOSTS
                or final.username is not None
                or final.password is not None
                or content_length is not None
                and (
                    not content_length.isdecimal()
                    or int(content_length) > MAX_SIGNED_ENVELOPE_BYTES
                )
            ):
                raise ReleaseAuthorityError("sandbox_release_channel_invalid")
            raw = response.read(MAX_SIGNED_ENVELOPE_BYTES + 1)
    except ReleaseAuthorityError:
        raise
    except (OSError, urlerror.HTTPError, urlerror.URLError) as exc:
        raise ReleaseAuthorityError(
            "sandbox_product_enablement_download_failed"
        ) from exc
    if len(raw) > MAX_SIGNED_ENVELOPE_BYTES:
        raise ReleaseAuthorityError("sandbox_product_enablement_download_failed")
    return raw


def _read_private_signed_bytes(path, *, error_code):
    path = Path(path)
    if not path.is_absolute() or not path.name:
        raise ReleaseAuthorityError(error_code)
    root = path.parent
    parent_descriptor = -1
    descriptor = -1
    try:
        root_identity = securitylib.private_directory_identity(root)
        path, parent_descriptor = securitylib._open_private_parent(
            path,
            trusted_root=root,
            trusted_root_identity=root_identity,
        )
        descriptor = os.open(
            path.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_descriptor,
        )
        before = os.fstat(descriptor)
        uid = os.geteuid() if hasattr(os, "geteuid") else before.st_uid
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != uid
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > MAX_SIGNED_ENVELOPE_BYTES
        ):
            raise ValueError("invalid product enablement cache")
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise ValueError("product enablement cache changed")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("product enablement cache changed")
        after = os.fstat(descriptor)
        current = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _entry_identity(before) != _entry_identity(after) or _entry_identity(
            after
        ) != _entry_identity(current):
            raise ValueError("product enablement cache changed")
        if securitylib.private_directory_identity(root) != root_identity:
            raise ValueError("private root changed")
        return b"".join(chunks)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReleaseAuthorityError(error_code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def read_candidate_attestation(path):
    raw = _read_private_signed_bytes(
        path,
        error_code="sandbox_candidate_attestation_invalid",
    )
    try:
        return decode_json(raw)
    except ReleaseAuthorityError as exc:
        raise ReleaseAuthorityError("sandbox_candidate_attestation_invalid") from exc


def _read_cached_bytes(root):
    return _read_private_signed_bytes(
        Path(root) / PRODUCT_ENABLEMENT_CACHE_NAME,
        error_code="sandbox_product_not_enabled",
    )


def _product_cache_lock_path(root):
    return Path(root).parent / ("." + Path(root).name + ".product-enablement.lock")


def _cached_product_sequence_floor(root):
    path = Path(root) / PRODUCT_ENABLEMENT_CACHE_NAME
    try:
        raw = _read_cached_bytes(root)
    except ReleaseAuthorityError as exc:
        try:
            path.lstat()
        except FileNotFoundError:
            return None, None
        except OSError as path_error:
            raise ReleaseAuthorityError(
                "sandbox_product_enablement_invalid"
            ) from path_error
        raise ReleaseAuthorityError("sandbox_product_enablement_invalid") from exc
    try:
        envelope = decode_json(raw)
        issued_at = _timestamp(envelope.get("issued_at"))
        payload = verify_signed_envelope(
            envelope,
            purpose=PRODUCT_ENABLEMENT_PURPOSE,
            now=issued_at,
        )
        validate_product_enablement_payload(payload)
        return envelope, payload
    except ReleaseAuthorityError as exc:
        raise ReleaseAuthorityError("sandbox_product_enablement_invalid") from exc


def load_cached_product_enablement(
    *,
    package_root,
    distribution_version,
    image,
    cache_root=None,
    now=None,
):
    envelope, _payload = load_cached_product_envelope(
        cache_root=cache_root,
        now=now,
    )
    try:
        return verify_product_enablement(
            envelope,
            package_root=package_root,
            distribution_version=distribution_version,
            image=image,
            now=now,
        )
    except ReleaseAuthorityError as exc:
        if exc.code == "release_attestation_expired":
            raise
        raise ReleaseAuthorityError("sandbox_product_enablement_invalid") from exc


def load_cached_product_envelope(*, cache_root=None, now=None):
    raw = _read_cached_bytes(cache_root or product_enablement_cache_root())
    try:
        envelope = decode_json(raw)
        payload = verify_signed_envelope(
            envelope,
            purpose=PRODUCT_ENABLEMENT_PURPOSE,
            now=now,
        )
        validate_product_enablement_payload(payload)
        return envelope, payload
    except ReleaseAuthorityError as exc:
        if exc.code == "release_attestation_expired":
            raise
        raise ReleaseAuthorityError("sandbox_product_enablement_invalid") from exc


def cache_product_enablement(
    raw,
    *,
    package_root,
    distribution_version,
    image,
    cache_root=None,
    now=None,
):
    envelope = decode_json(raw)
    payload = verify_product_enablement(
        envelope,
        package_root=package_root,
        distribution_version=distribution_version,
        image=image,
        now=now,
    )
    root = securitylib.ensure_private_dir(
        cache_root or product_enablement_cache_root()
    )
    with file_lock.locked_file(
        _product_cache_lock_path(root),
        require_lock=True,
    ):
        current, current_payload = _cached_product_sequence_floor(root)
        if current_payload is not None and (
            payload["release_sequence"] < current_payload["release_sequence"]
            or payload["release_sequence"] == current_payload["release_sequence"]
            and attestation_digest(envelope) != attestation_digest(current)
        ):
            raise ReleaseAuthorityError("sandbox_product_enablement_rollback")
        rendered = canonical_json(envelope) + b"\n"
        securitylib.write_private_bytes_atomic(
            root / PRODUCT_ENABLEMENT_CACHE_NAME,
            rendered,
            trusted_root=root,
            trusted_root_identity=securitylib.private_directory_identity(root),
            max_existing_bytes=MAX_SIGNED_ENVELOPE_BYTES,
        )
    return payload
